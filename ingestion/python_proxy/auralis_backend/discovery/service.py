from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Dict
import json
import threading
import time
import uuid

from fastapi import HTTPException

from ..recommend.session_runtime import _store_feed_session, load_feed_session
from ..search.catalog_pipeline import schedule_catalog_population
from ..search.intelligence import remember_catalog_entity
from ..storage.artist_artwork import schedule_artist_artwork_cache
from .adapters import (
    artifact_to_session,
    home_response_from_artifact,
    row_page_response_from_artifact,
)
from .artifact import build_diagnostics, evaluate_quality
from .config import (
    ARTIFACT_TTL_SECONDS,
    ARTIFACT_VERSION,
    ENGINE_MODEL_VERSION,
    FEED_PROMOTION_CONTRACT_VERSION,
    ROW_ORDER,
    ROW_RECIPES,
)
from .feed_state import (
    FeedState,
    feed_queue_summary,
    feed_queue_condition,
    load_feed_state,
    mark_feed_build_failed,
    mark_feed_dirty,
    mark_feed_replenishing,
    promote_prepared_feed,
    retain_compatible_ready_feeds,
    save_feed_state,
    store_active_feed,
    store_prepared_feed,
    _feed_content_signature,
    _row_content_signatures,
)
from .enrichment import (
    build_enrichment_plan,
    complete_inventory_release_metadata,
    hydrate_artifact_release_metadata,
    materialize_enrichment_plan,
)
from .inventory import (
    CandidateInventory,
    apply_inventory_intent_delta,
    build_candidate_inventory,
    candidate_inventory_coverage,
    canonical_item_identity,
    clear_inventory_intent_delta,
    load_candidate_inventory,
    load_inventory_intent_delta,
    inventory_with_row_shortages,
    refresh_candidate_inventory_coverage,
    store_candidate_inventory,
)
from .radio_inventory import (
    RADIO_RESERVOIR_TARGET_CARDS,
    build_artist_radio_inventory,
    merge_radio_reservoirs,
    select_radio_rotation,
    load_artist_radio_inventory,
    merge_store_artist_radio_inventory,
    radio_card_candidates,
    store_artist_radio_inventory,
)
from .ranking import build_rows_from_pools
from .schema import DiscoveryArtifact
from .signals import build_taste_profile
from .source_registry import verify_materialized_supply

_OPTIONAL_ROW_KINDS = {
    "featured_new_albums",
    "popular_radio",
    "recommended_albums",
}


def _radio_row_has_verified_artwork(row: Any) -> bool:
    if getattr(row, "kind", "") != "popular_radio":
        return True
    items = list(getattr(row, "items", []) or [])
    valid_items = [item for item in items if isinstance(item, dict)]
    return bool(valid_items) and len(valid_items) == len(items) and all(
        str(item.get("thumbnail") or "").strip().startswith(
            "/artist_artwork/"
        )
        and any(
            str(value or "").strip().startswith("/artist_artwork/")
            for value in item.get("collage_images") or []
        )
        for item in valid_items
    )


def _radio_inventory_matches_successor(
    inventory: Any,
    *,
    profile_fingerprint: str,
    successor_epoch: int,
) -> bool:
    if inventory is None or not inventory.is_ready:
        return False
    if str(inventory.profile_fingerprint or "") != str(profile_fingerprint or ""):
        return False
    diagnostics = dict(inventory.diagnostics or {})
    inventory_epoch = int(
        diagnostics.get("successor_rotation_epoch")
        or diagnostics.get("rotation_epoch")
        or -1
    )
    return inventory_epoch == max(int(successor_epoch or 0), 0)


def _artifact_row_kinds(artifact: DiscoveryArtifact | None) -> set[str]:
    return {row.kind for row in (artifact.rows if artifact is not None else [])}


def _completed_optional_rows(coverage: Dict[str, Any] | None) -> set[str]:
    actual = dict((coverage or {}).get("actual") or {})
    return {
        kind
        for kind in _OPTIONAL_ROW_KINDS
        if int(actual.get(kind) or 0) >= ROW_RECIPES[kind].min_items
    }


def _preserve_complete_optional_rows(
    rows: list[Any],
    active: DiscoveryArtifact | None,
) -> tuple[list[Any], list[str]]:
    if active is None:
        return rows, []
    output = list(rows or [])
    present = {row.kind for row in output}
    preserved: list[str] = []
    for row in active.rows or []:
        if row.kind not in _OPTIONAL_ROW_KINDS or row.kind in present:
            continue
        if len(row.items or []) < ROW_RECIPES[row.kind].min_items:
            continue
        if not _radio_row_has_verified_artwork(row):
            continue
        output.append(row)
        present.add(row.kind)
        preserved.append(row.kind)
    order = {kind: index for index, kind in enumerate(ROW_ORDER)}
    output.sort(key=lambda row: order.get(row.kind, len(order)))
    return output, preserved


class DiscoveryService:
    def __init__(self, server: Any) -> None:
        self._server = server
        self._prepare_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="auralis-feed-prepare",
        )
        self._metadata_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="auralis-release-metadata",
        )
        self._radio_artwork_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="auralis-radio-artwork-dispatch",
        )
        self._radio_reservoir_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="auralis-radio-reservoir",
        )
        self._last_background_builds: Dict[str, float] = {}
        self._background_builds_inflight: set[str] = set()
        self._background_build_tokens: Dict[str, str] = {}
        self._pending_background_builds: Dict[str, tuple[Any, Any, str]] = {}
        self._signal_reconciliations_inflight: set[str] = set()
        self._radio_reservoir_inflight: set[str] = set()
        self._background_build_lock = threading.Lock()
        self._stored_session_ids: set[str] = set()
        self._stored_session_lock = threading.Lock()

    def _background_fingerprint(self, taste: Any) -> str:
        # Inventory is persisted per user, so preparation must also serialize
        # per user. Profile-keyed workers could otherwise overwrite one another.
        return str(taste.user_scope_id or "guest")

    def _artifact_signature(self, artifact: DiscoveryArtifact | None) -> str:
        if artifact is None:
            return ""
        return _feed_content_signature(artifact)

    def _schedule_preparation_after_response(
        self,
        req: Any,
        *,
        reason: str,
        dedupe_key: str = "",
    ) -> None:
        """Reconcile current signals without extending a feed response."""

        scope = str(getattr(req, "user_scope_id", "") or "guest")
        state = load_feed_state(self._server, scope)
        if state is not None and len(state.ready_feeds or []) >= 2:
            # A full queue is terminal for replenishment; do not even occupy
            # the reconciliation executor for a stale callback. Preserve any
            # active lease so another worker can clean up its own marker.
            return

        if dedupe_key:
            with self._background_build_lock:
                if dedupe_key in self._signal_reconciliations_inflight:
                    return
                self._signal_reconciliations_inflight.add(dedupe_key)

        def reconcile() -> None:
            try:
                taste = build_taste_profile(self._server, req)
                self._schedule_preparation(req, taste, reason=reason)
            except Exception:
                return
            finally:
                if dedupe_key:
                    with self._background_build_lock:
                        self._signal_reconciliations_inflight.discard(dedupe_key)

        try:
            self._prepare_executor.submit(reconcile)
        except Exception:
            if dedupe_key:
                with self._background_build_lock:
                    self._signal_reconciliations_inflight.discard(dedupe_key)
            return

    def _refresh_work_is_running(self, scope: str, req: Any) -> bool:
        refresh_token = str(getattr(req, "refresh_token", "") or "").strip()
        if not refresh_token:
            return False
        reconcile_key = f"refresh:{scope}:{refresh_token}"
        with self._background_build_lock:
            return bool(
                reconcile_key in self._signal_reconciliations_inflight
                or (
                    scope in self._background_builds_inflight
                    and self._background_build_tokens.get(scope) == refresh_token
                )
            )

    def _wait_for_prepared_feed(
        self,
        scope: str,
        state: FeedState,
        *,
        request_id: str,
        wait_ms: int,
    ) -> tuple[FeedState, DiscoveryArtifact | None, bool, int]:
        """Wait for one queue append without polling/rebuilding the feed."""
        bounded_ms = max(0, min(int(wait_ms or 0), 3000))
        started = time.perf_counter()
        if bounded_ms <= 0:
            return state, None, False, 0
        condition = feed_queue_condition(scope)
        deadline = time.monotonic() + bounded_ms / 1000.0
        while True:
            current = load_feed_state(self._server, scope) or state
            if current.ready_feeds or current.prepared_feed is not None:
                promoted = promote_prepared_feed(self._server, current)
                if promoted is not None:
                    print(
                        "[EBB:feed-queue] wait "
                        f"scope={scope} outcome=promoted wait_ms={int((time.perf_counter()-started)*1000)}",
                        flush=True,
                    )
                    return current, promoted, True, int((time.perf_counter() - started) * 1000)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with condition:
                # Recheck immediately after acquiring the condition to avoid
                # losing a notify between the load and wait.
                current = load_feed_state(self._server, scope) or state
                if current.ready_feeds or current.prepared_feed is not None:
                    continue
                condition.wait(timeout=remaining)
        elapsed = int((time.perf_counter() - started) * 1000)
        print(
            "[EBB:feed-queue] wait "
            f"scope={scope} outcome=timeout wait_ms={elapsed}",
            flush=True,
        )
        return load_feed_state(self._server, scope) or state, None, False, elapsed

    @staticmethod
    def _requested_refresh_wait_ms(req: Any) -> int:
        value = getattr(req, "feed_refresh_wait_ms", None)
        if value in (None, "", 0):
            value = getattr(req, "refresh_wait_ms", None)
        if value in (None, ""):
            value = 2500
        return max(0, min(int(value or 0), 3000))

    def _changed_row_kinds(
        self,
        previous: DiscoveryArtifact | None,
        current: DiscoveryArtifact,
    ) -> list[str]:
        previous_rows = _row_content_signatures(previous)
        current_rows = _row_content_signatures(current)
        return [
            kind
            for kind, signature in current_rows.items()
            if previous_rows.get(kind) != signature
        ]

    def _claim_background_build(
        self,
        fingerprint: str,
        *,
        urgent: bool = False,
        refresh_token: str = "",
    ) -> bool:
        with self._background_build_lock:
            last_build = self._last_background_builds.get(fingerprint, 0.0)
            if fingerprint in self._background_builds_inflight or (
                not urgent and time.time() - last_build < 45.0
            ):
                return False
            self._background_builds_inflight.add(fingerprint)
            self._background_build_tokens[fingerprint] = refresh_token
            return True

    def _release_background_build(self, fingerprint: str) -> None:
        with self._background_build_lock:
            self._background_builds_inflight.discard(fingerprint)
            self._background_build_tokens.pop(fingerprint, None)
            self._last_background_builds[fingerprint] = time.time()

    def _same_refresh_build_is_running(self, taste: Any, req: Any) -> bool:
        refresh_token = str(getattr(req, "refresh_token", "") or "").strip()
        if not refresh_token:
            return False
        fingerprint = self._background_fingerprint(taste)
        with self._background_build_lock:
            return (
                fingerprint in self._background_builds_inflight
                and self._background_build_tokens.get(fingerprint) == refresh_token
            )

    def _visible_track_ids(self, artifact: DiscoveryArtifact | None) -> list[str]:
        if artifact is None:
            return []
        output: list[str] = []
        seen: set[str] = set()

        def add_item(item: Dict[str, Any]) -> None:
            item_id = str(item.get("id") or item.get("videoId") or "").strip()
            if item_id and item_id not in seen:
                seen.add(item_id)
                output.append(item_id)

        for row in artifact.rows or []:
            for item in list(row.items or [])[:12]:
                add_item(item)
                nested = item.get("tracks") or item.get("items")
                if isinstance(nested, list):
                    for track in nested[:12]:
                        if isinstance(track, dict):
                            add_item(track)
        return output

    def _rotation_taste(
        self,
        taste: Any,
        active: DiscoveryArtifact | None,
        *,
        prior_artifacts: list[DiscoveryArtifact] | None = None,
        reason: str,
        rotation_epoch: int = 0,
        inventory_generation: str = "",
    ) -> Any:
        avoid_ids = list(
            dict.fromkeys(
                [
                    *list(getattr(taste, "avoid_ids", []) or []),
                    *self._visible_track_ids(active),
                    *[
                        track_id
                        for artifact in list(prior_artifacts or [])
                        for track_id in self._visible_track_ids(artifact)
                    ],
                ]
            )
        )
        return replace(
            taste,
            avoid_ids=avoid_ids,
            force_refresh=True,
            rotation_epoch=max(int(rotation_epoch or 0), 0),
            refresh_token=(
                f"{taste.user_scope_id}:{taste.profile_key}:"
                f"{inventory_generation}:{max(int(rotation_epoch or 0), 0)}:{reason}"
            ),
        )

    def recommend(
        self,
        req: Any,
        *,
        request_mode: str,
        trace: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return self._recommend_v2(req, request_mode=request_mode, trace=trace)

    def _recommend_v2(
        self,
        req: Any,
        *,
        request_mode: str,
        trace: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        request_id = str((trace or {}).get("request_id") or uuid.uuid4())
        page_size = max(
            int(getattr(self._server, "RECOMMENDATION_ROW_PAGE_SIZE", 8) or 8),
            1,
        )
        if request_mode == "row_page":
            return self._row_page(req, request_id=request_id)

        lifecycle_started = time.perf_counter()
        force_requested = bool(
            getattr(req, "force_refresh", False)
            or getattr(req, "prefer_fresh_rows", False)
        )
        session_requested = bool(getattr(req, "session_intent", False))
        # The client marks only its initial app-load request. Generic
        # non-force requests during an active session must continue serving
        # the current active feed; otherwise every poll would rotate FIFO.
        launch_requested = bool(getattr(req, "promote_ready_on_launch", False))
        scope = self._trim(getattr(req, "user_scope_id", "")) or "guest"

        if request_mode == "queue_status" or bool(
            getattr(req, "feed_queue_status_only", False)
        ):
            # Queue status is metadata-only and supports a bounded long poll.
            # The passive path never hydrates artifacts or starts a build.
            summary = feed_queue_summary(self._server, scope)
            last_revision = max(
                int(
                    getattr(req, "feed_queue_revision", 0)
                    or getattr(req, "last_seen_queue_revision", 0)
                    or 0
                ),
                0,
            )
            wait_ms = min(
                max(
                    int(
                        getattr(req, "feed_queue_wait_ms", 0)
                        or getattr(req, "queue_wait_ms", 0)
                        or 0
                    ),
                    0,
                ),
                9000,
            )
            condition = feed_queue_condition(scope)
            retry_scheduled = False
            if (
                summary.get("preparation_state") in {"preparing", "inventory_building"}
                and float(summary.get("preparation_lease_deadline") or 0.0) <= time.time()
            ):
                # The normal path remains metadata-only. An expired lease is
                # exceptional and must pass through the existing recovery
                # routine once so it cannot look in-flight forever.
                stale_state = load_feed_state(self._server, scope)
                if stale_state is not None:
                    self._queue_state_diagnostics(stale_state)
                summary = feed_queue_summary(self._server, scope)
            # Due retry semantics are the sole passive exception: re-arm
            # immediately before waiting so a stale delayed state does not
            # incur the full long-poll timeout.
            if (
                summary["preparation_state"] == "delayed"
                and summary["retry_at"]
                and summary["retry_at"] <= time.time()
            ):
                retry_state = load_feed_state(self._server, scope)
                if retry_state is not None:
                    taste = build_taste_profile(self._server, req)
                    reason = str(retry_state.retry_reason or "").strip() or "queue_replenish"
                    self._schedule_preparation(req, taste, reason=reason)
                    retry_scheduled = True
                    summary = feed_queue_summary(self._server, scope)
            deadline = time.monotonic() + wait_ms / 1000.0
            waited = False
            wait_timed_out = False
            while (
                wait_ms
                and summary["queue_revision"] == last_revision
                and summary["ready_feed_depth"] < 2
                and (
                    summary.get("queue_phase")
                    in {
                        "building",
                        "preparing",
                        "inventory_building",
                        "scheduled",
                        "prepared",
                        "delayed",
                        "retry",
                    }
                    or summary.get("queue_build_inflight") is True
                )
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    wait_timed_out = True
                    break
                with condition:
                    condition.wait(timeout=min(remaining, 0.5))
                waited = True
                summary = feed_queue_summary(self._server, scope)
                if summary["queue_revision"] != last_revision or summary["ready_feed_depth"] >= 2:
                    break
            status_state = None
            if (
                not retry_scheduled
                and summary["preparation_state"] == "delayed"
                and summary["retry_at"]
                and summary["retry_at"] <= time.time()
            ):
                status_state = load_feed_state(self._server, scope)
                taste = build_taste_profile(self._server, req)
                reason = str(getattr(status_state, "retry_reason", "") or "").strip() or "queue_replenish"
                self._schedule_preparation(req, taste, reason=reason)
            response = self._queue_status_response(state=status_state, request_id=request_id, summary=summary)
            diagnostics = response["diagnostics"]
            diagnostics["queue_status_waited"] = waited
            diagnostics["queue_status_timed_out"] = wait_timed_out
            return response

        # Returning from Search/session intent never promotes or rebuilds.
        if request_mode == "full_feed" and session_requested and not force_requested:
            session_state = load_feed_state(self._server, scope)
            if session_state is not None:
                promoted = None
                visible = session_state.active_feed
                if visible is not None:
                    self._schedule_preparation_after_response(
                        req,
                        reason="search_session_intent",
                        dedupe_key=f"search-intent:{scope}",
                    )
                    return self._feed_response(
                        visible,
                        state=session_state,
                        request_id=request_id,
                        page_size=page_size,
                        action="served_active",
                        changed=False,
                        reason="search_intent_no_promotion",
                    )

        # Pull-to-refresh normally promotes a successor that has already been
        # validated and persisted. That promotion does not need a fresh taste
        # rebuild in the request path. Reconciliation prepares the following
        # successor after this response has been returned.
        if request_mode == "full_feed" and force_requested:
            refresh_state = load_feed_state(self._server, scope)
            if refresh_state is not None and refresh_state.prepared_feed is not None:
                promoted = promote_prepared_feed(self._server, refresh_state)
                if promoted is not None:
                    intent_version = int(
                        (promoted.diagnostics or {}).get("intent_delta_version") or 0
                    )
                    if intent_version > 0:
                        clear_inventory_intent_delta(
                            self._server,
                            scope,
                            consumed_version=intent_version,
                        )
                    self._schedule_preparation_after_response(
                        req,
                        reason="post_promotion",
                    )
                    return self._feed_response(
                        promoted,
                        state=refresh_state,
                        request_id=request_id,
                        page_size=page_size,
                        action="promoted_prepared",
                        changed=True,
                    )
            if refresh_state is not None and refresh_state.active_feed is not None:
                refresh_token = str(getattr(req, "refresh_token", "") or "").strip()
                reconcile_key = f"refresh:{scope}:{refresh_token}"
                if not self._refresh_work_is_running(scope, req):
                    self._schedule_preparation_after_response(
                        req,
                        reason="pull_to_refresh",
                        dedupe_key=reconcile_key,
                    )
                waited_state, waited_promoted, waited_changed, waited_ms = (
                    self._wait_for_prepared_feed(
                        scope,
                        refresh_state,
                        request_id=request_id,
                        wait_ms=self._requested_refresh_wait_ms(req),
                    )
                )
                if waited_promoted is not None:
                    self._schedule_preparation_after_response(
                        req,
                        reason="post_promotion",
                        dedupe_key=f"refresh-promotion:{scope}",
                    )
                    return self._feed_response(
                        waited_promoted,
                        state=waited_state,
                        request_id=request_id,
                        page_size=page_size,
                        action="promoted_prepared",
                        changed=True,
                        reason="refresh_wait_promoted",
                    )
                response = self._feed_response(
                    waited_state.active_feed,
                    state=waited_state,
                    request_id=request_id,
                    page_size=page_size,
                    action="unchanged_no_rotation",
                    changed=False,
                    reason="refresh_preparing_timeout",
                )
                diagnostics = response.setdefault("diagnostics", {})
                diagnostics.update(
                    {
                        "refresh_wait_requested_ms": self._requested_refresh_wait_ms(req),
                        "refresh_wait_ms": waited_ms,
                        "refresh_wait_outcome": "timeout",
                        "preparation_state": "preparing",
                    }
                )
                return response

        # Serving an existing feed does not require rebuilding the complete
        # taste profile. History reconciliation and successor preparation can
        # run after the response instead of delaying every cold launch.
        if (
            request_mode == "full_feed"
            and not force_requested
            and not session_requested
            and launch_requested
        ):
            launch_state = load_feed_state(self._server, scope)
            if launch_state is not None and launch_state.active_feed is not None:
                # A ready successor is the durable launch snapshot. Promote
                # exactly one FIFO entry so a restart/launch cannot keep
                # serving an older active feed while a validated successor
                # waits in storage. Search/session requests are handled above
                # and intentionally never reach this path.
                launch_token = str(getattr(req, "launch_token", "") or "").strip()
                already_consumed = bool(
                    launch_token
                    and str(
                        (launch_state.active_feed.diagnostics or {}).get(
                            "launch_token", ""
                        )
                    )
                    == launch_token
                )
                if launch_state.ready_feeds and not already_consumed:
                    promoted = promote_prepared_feed(self._server, launch_state)
                    if promoted is not None:
                        if launch_token:
                            promoted.diagnostics = dict(promoted.diagnostics or {})
                            promoted.diagnostics["launch_token"] = launch_token
                            save_feed_state(
                                self._server,
                                launch_state,
                                expected_active_version=launch_state.active_version,
                                expected_ready_session_ids=[
                                    entry.session_id
                                    for entry in launch_state.ready_feeds or []
                                ],
                            )
                        self._schedule_preparation_after_response(
                            req,
                            reason="post_launch_promotion",
                            dedupe_key=f"launch:{scope}",
                        )
                        return self._feed_response(
                            promoted,
                            state=launch_state,
                            request_id=request_id,
                            page_size=page_size,
                            action="promoted_prepared",
                            changed=True,
                            reason="launch_ready_fifo",
                        )
            # A launch request with no successor still serves active and
            # schedules replenishment below.

        if (
            request_mode == "full_feed"
            and not force_requested
            and not session_requested
        ):
            launch_state = load_feed_state(self._server, scope)
            if launch_state is not None and launch_state.active_feed is not None:
                needs_successor = bool(
                    (
                        len(launch_state.ready_feeds or []) < 2
                        or launch_state.dirty_reasons
                    )
                    and launch_state.dirty_reasons != ["rotation_inventory_exhausted"]
                )
                if needs_successor:
                    self._schedule_preparation_after_response(
                        req,
                        reason="launch_missing_successor",
                        dedupe_key=f"launch:{scope}",
                    )
                launch_state.active_feed.diagnostics = dict(
                    launch_state.active_feed.diagnostics or {}
                )
                launch_state.active_feed.diagnostics["feed_active_hit_ms"] = int(
                    (time.perf_counter() - lifecycle_started) * 1000
                )
                return self._feed_response(
                    launch_state.active_feed,
                    state=launch_state,
                    request_id=request_id,
                    page_size=page_size,
                    action="served_active",
                    changed=False,
                )

        taste = build_taste_profile(self._server, req)
        state = load_feed_state(self._server, taste.user_scope_id)
        if state is None:
            state = FeedState(user_scope_id=taste.user_scope_id)
            save_feed_state(self._server, state)

        force_refresh = force_requested
        session_intent = session_requested
        if (
            state.active_feed is not None
            and state.profile_fingerprint
            and state.profile_fingerprint != taste.profile_key
        ):
            retain_compatible_ready_feeds(
                self._server,
                state,
                taste.profile_key,
            )
            mark_feed_dirty(self._server, state, "profile_changed")
        if force_refresh:
            mark_feed_dirty(self._server, state, "pull_to_refresh")

        if force_refresh or session_intent:
            promoted = (
                promote_prepared_feed(self._server, state)
                if force_refresh and not session_intent
                else None
            )
            if promoted is not None:
                intent_version = int(
                    (promoted.diagnostics or {}).get("intent_delta_version") or 0
                )
                if intent_version > 0:
                    clear_inventory_intent_delta(
                        self._server,
                        taste.user_scope_id,
                        consumed_version=intent_version,
                    )
                self._schedule_preparation(req, taste, reason="post_promotion")
                return self._feed_response(
                    promoted,
                    state=state,
                    request_id=request_id,
                    page_size=page_size,
                    action="promoted_prepared",
                    changed=True,
                )

            # A client waiting on the same explicit refresh only needs the
            # current status. Rebuilding the same inventory on every poll
            # wastes work and used to queue a false successor request.
            if (
                force_refresh
                and self._same_refresh_build_is_running(taste, req)
                and state.active_feed is not None
            ):
                return self._feed_response(
                    state.active_feed,
                    state=state,
                    request_id=request_id,
                    page_size=page_size,
                    action="unchanged_no_rotation",
                    changed=False,
                    reason="refresh_in_progress",
                )

            # Pull-to-refresh is a promotion request, not a feed-construction
            # request.  If no successor is ready, keep the active feed visible
            # and let the existing client poll observe the background result.
            if force_refresh and not session_intent:
                self._schedule_preparation(req, taste, reason="pull_to_refresh")
                if state.active_feed is not None:
                    return self._feed_response(
                        state.active_feed,
                        state=state,
                        request_id=request_id,
                        page_size=page_size,
                        action="unchanged_no_rotation",
                        changed=False,
                        reason="refresh_preparing_successor",
                    )
                return self._preparing_response(
                    taste=taste,
                    state=state,
                    request_id=request_id,
                    page_size=page_size,
                )

            inventory = load_candidate_inventory(
                self._server,
                taste.user_scope_id,
                profile_fingerprint="" if session_intent else taste.profile_key,
                require_fresh=False,
            )
            if inventory is not None:
                if session_intent:
                    inventory = apply_inventory_intent_delta(
                        inventory,
                        load_inventory_intent_delta(self._server, taste.user_scope_id),
                    )
                rotation_taste = self._rotation_taste(
                    taste,
                    state.active_feed,
                    reason="search_intent" if session_intent else "pull_refresh",
                    rotation_epoch=state.rotation_epoch + 1,
                    inventory_generation=inventory.generation_id,
                )
                candidate = self._build_artifact(
                    req,
                    taste=rotation_taste,
                    inventory=inventory,
                    artifact_source="inventory_rotation",
                    request_id=request_id,
                )
                changed_rows = self._changed_row_kinds(state.active_feed, candidate)
                changed = bool(changed_rows)
                if candidate.accepted and changed:
                    candidate.diagnostics["changed_row_kinds"] = changed_rows
                    stored = store_active_feed(
                        self._server,
                        state,
                        candidate,
                        profile_fingerprint=taste.profile_key,
                        inventory_generation=inventory.generation_id,
                        rotation_epoch=state.rotation_epoch + 1,
                        expected_active_version=state.active_version,
                    )
                    if stored is None:
                        changed = False
                    else:
                        if inventory.intent_version > 0:
                            clear_inventory_intent_delta(
                                self._server,
                                taste.user_scope_id,
                                consumed_version=inventory.intent_version,
                            )
                    if not changed:
                        self._schedule_preparation(req, taste, reason="pull_to_refresh")
                        current = (
                            load_feed_state(self._server, taste.user_scope_id) or state
                        )
                        if current.active_feed is not None:
                            return self._feed_response(
                                current.active_feed,
                                state=current,
                                request_id=request_id,
                                page_size=page_size,
                                action="unchanged_no_rotation",
                                changed=False,
                                reason="active_version_changed_during_rotation",
                            )
                    self._schedule_preparation(req, taste, reason="post_refresh")
                    return self._feed_response(
                        candidate,
                        state=state,
                        request_id=request_id,
                        page_size=page_size,
                        action="built_and_promoted",
                        changed=True,
                    )

            self._schedule_preparation(
                req,
                taste,
                reason="search_session_intent" if session_intent else "pull_to_refresh",
            )
            if state.active_feed is not None:
                return self._feed_response(
                    state.active_feed,
                    state=state,
                    request_id=request_id,
                    page_size=page_size,
                    action="unchanged_no_rotation",
                    changed=False,
                    reason="candidate_inventory_not_ready_or_unchanged",
                )
            return self._preparing_response(
                taste=taste,
                state=state,
                request_id=request_id,
                page_size=page_size,
            )

        if request_mode == "background_prepare":
            # This mode is a scheduler/status probe. Artifact construction is
            # owned by the bounded background worker; rebuilding here made an
            # ostensibly asynchronous request block on inventory and provider
            # work, and could race the worker's CAS promotion.
            self._schedule_preparation(req, taste, reason="background_prepare")
            if state.active_feed is not None:
                return self._feed_response(
                    state.active_feed,
                    state=state,
                    request_id=request_id,
                    page_size=page_size,
                    action="served_active",
                    changed=False,
                )
            return self._preparing_response(
                taste=taste,
                state=state,
                request_id=request_id,
                page_size=page_size,
            )

        if state.active_feed is None and state.prepared_feed is not None:
            promoted = promote_prepared_feed(self._server, state)
            if promoted is not None:
                intent_version = int(
                    (promoted.diagnostics or {}).get("intent_delta_version") or 0
                )
                if intent_version > 0:
                    clear_inventory_intent_delta(
                        self._server,
                        taste.user_scope_id,
                        consumed_version=intent_version,
                    )
                self._schedule_preparation(req, taste, reason="post_promotion")
                return self._feed_response(
                    promoted,
                    state=state,
                    request_id=request_id,
                    page_size=page_size,
                    action="promoted_prepared",
                    changed=True,
                )

        if state.active_feed is not None:
            inventory = load_candidate_inventory(
                self._server,
                taste.user_scope_id,
                profile_fingerprint=taste.profile_key,
            )
            prepared_matches_profile = bool(
                state.prepared_feed is not None
                and state.prepared_feed.profile_key == taste.profile_key
            )
            rotation_exhausted = state.dirty_reasons == ["rotation_inventory_exhausted"]
            if inventory is None or (
                not prepared_matches_profile and not rotation_exhausted
            ):
                self._schedule_preparation(
                    req,
                    taste,
                    reason=(
                        "launch_stale_inventory"
                        if inventory is None
                        or state.profile_fingerprint != taste.profile_key
                        else "launch_missing_successor"
                    ),
                )
            state.active_feed.diagnostics = dict(state.active_feed.diagnostics or {})
            state.active_feed.diagnostics["feed_active_hit_ms"] = int(
                (time.perf_counter() - lifecycle_started) * 1000
            )
            return self._feed_response(
                state.active_feed,
                state=state,
                request_id=request_id,
                page_size=page_size,
                action="served_active",
                changed=False,
            )

        # A terminal attempt may fail because a provider batch was partial or
        # temporarily unavailable.  Keep the exact failure reason in state,
        # but let the normal 45-second worker guard resume persisted progress;
        # otherwise one failed cold-start attempt bricks the feed forever.
        self._schedule_preparation(
            req,
            taste,
            reason=(
                "initial_retry"
                if state.generation_status == "build_failed"
                else "initial_feed"
            ),
        )
        return self._preparing_response(
            taste=taste,
            state=state,
            request_id=request_id,
            page_size=page_size,
        )

    def _feed_response(
        self,
        artifact: DiscoveryArtifact,
        *,
        state: FeedState,
        request_id: str,
        page_size: int,
        action: str,
        changed: bool,
        reason: str = "",
    ) -> Dict[str, Any]:
        queue_diagnostics = self._queue_state_diagnostics(state)
        artifact.diagnostics = dict(artifact.diagnostics or {})
        artifact.diagnostics.update(
            {
                "feed_action": action,
                "feed_state_version": "feed-state",
                "feed_version": state.active_version,
                **queue_diagnostics,
                "refresh_changed": bool(changed),
                "quality_warnings": list(artifact.quality_reasons or []),
                # Compatibility mirrors for one client transition release.
                "artifact_status": "servable" if artifact.accepted else "build_failed",
                "artifact_quality": "servable" if artifact.accepted else "build_failed",
                "refresh_outcome": action,
            }
        )
        if reason:
            artifact.diagnostics["feed_action_reason"] = reason
        artifact.artifact_source = "cache" if action == "served_active" else action
        self._store_session(artifact)
        return home_response_from_artifact(
            artifact,
            request_id=request_id,
            page_size=page_size,
        )

    def _queue_state_diagnostics(self, state: FeedState) -> Dict[str, Any]:
        # Diagnostics may be requested with a stale in-memory snapshot (for
        # example immediately after restart). Always reconcile against the
        # durable state before evaluating lease expiry.
        durable = load_feed_state(self._server, state.user_scope_id)
        if durable is not None:
            state = durable
        with self._background_build_lock:
            scope_prefix = f"{state.user_scope_id}:"
            build_inflight = any(
                str(key).startswith(scope_prefix)
                for key in self._background_builds_inflight
            )
            reconciliation_inflight = any(
                str(key).endswith(f":{state.user_scope_id}")
                for key in self._signal_reconciliations_inflight
            )
        stale_lease = False
        if (
            (
                str(state.preparation_lease_token or "").strip()
                or state.generation_status in {"preparing", "inventory_building"}
            )
            and (
                not state.preparation_lease_deadline
                or float(state.preparation_lease_deadline) <= time.time()
            )
        ):
            stale_reason = (
                str(state.preparation_lease_reason or "").strip()
                or str(state.retry_reason or "").strip()
                or "queue_replenish"
            )
            state.generation_status = "delayed"
            state.retry_at = time.time() + 5.0
            state.retry_reason = stale_reason
            state.preparation_lease_started_at = 0.0
            state.preparation_lease_deadline = 0.0
            state.preparation_lease_reason = ""
            # Clearing the durable token fences any old worker still blocked
            # in provider or storage I/O. It may finish, but cannot commit.
            state.preparation_lease_token = ""
            save_feed_state(self._server, state)
            stale_lease = True
        if stale_lease:
            self._release_background_build(str(state.user_scope_id or "guest"))
            build_inflight = False
        lease_active = bool(
            state.preparation_lease_deadline
            and float(state.preparation_lease_deadline) > time.time()
        )
        queue_build_inflight = bool(build_inflight or reconciliation_inflight or lease_active)
        queue_phase = (
            "building"
            if queue_build_inflight
            else str(state.generation_status or "idle")
        )
        ready_feeds = state.ready_feeds or (
            [] if state.prepared_feed is None else [state.prepared_feed]
        )
        return {
            "user_scope_id": state.user_scope_id,
            "preparation_state": state.generation_status,
            "ready_feed_count": len(ready_feeds),
            "ready_feed_depth": len(ready_feeds),
            "ready_feed_target_depth": 2,
            "ready_feed_session_ids": [
                str(item.session_id or "") for item in ready_feeds[:2]
            ],
            "queue_build_inflight": queue_build_inflight,
            "queue_phase": queue_phase,
            "preparation_lease": {
                "active": lease_active,
                "started_at": float(state.preparation_lease_started_at or 0.0),
                "deadline": float(state.preparation_lease_deadline or 0.0),
                "reason": str(state.preparation_lease_reason or ""),
            },
            "retry_at": float(state.retry_at or 0.0),
            "retry_reason": str(state.retry_reason or ""),
            "queue_last_rejection_or_shortage": next(
                (
                    str(reason)
                    for reason in reversed(state.dirty_reasons or [])
                    if str(reason).strip()
                ),
                "",
            ),
            "prepared_candidate_available": bool(ready_feeds),
            "rotation_inventory_exhausted": state.dirty_reasons
            == ["rotation_inventory_exhausted"],
        }

    def _queue_status_response(
        self,
        *,
        state: FeedState,
        request_id: str,
        summary: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        if summary is not None:
            diagnostics = {
                "engine": "discovery_engine", "feed_action": "queue_status",
                "feed_state_version": "feed-state", "feed_version": 0,
                **summary,
            }
            return {"status": "success", "request_id": request_id, "session_id": "", "feed_version": 0,
                    "feed_action": "queue_status", "preparation_state": diagnostics["preparation_state"],
                    "rows": [], "has_more": False, "next_offset": 0, "diagnostics": diagnostics}
        diagnostics = {
            "engine": "discovery_engine",
            "feed_action": "queue_status",
            "feed_state_version": "feed-state",
            "feed_version": state.active_version,
            **self._queue_state_diagnostics(state),
        }
        return {
            "status": "success",
            "request_id": request_id,
            "session_id": str(
                getattr(state.active_feed, "session_id", "") or ""
            ),
            "feed_version": state.active_version,
            "feed_action": "queue_status",
            "preparation_state": diagnostics["preparation_state"],
            "rows": [],
            "has_more": False,
            "next_offset": 0,
            "diagnostics": diagnostics,
        }

    def _preparing_response(
        self,
        *,
        taste: Any,
        state: FeedState,
        request_id: str,
        page_size: int,
    ) -> Dict[str, Any]:
        now = time.time()
        failure_reasons = [str(value or "") for value in state.dirty_reasons or []]
        build_failed = state.generation_status == "build_failed" and any(
            reason.startswith("prepare_exception:")
            or reason.startswith("artifact_rejected_without_candidate_shortage:")
            for reason in failure_reasons
        )
        action = "build_failed" if build_failed else "preparing_initial"
        artifact = DiscoveryArtifact(
            session_id=str(uuid.uuid4()),
            user_scope_id=taste.user_scope_id,
            profile_key=taste.profile_key,
            generated_at=now,
            expires_at=now + ARTIFACT_TTL_SECONDS,
            rows=[],
            diagnostics={
                "engine": "discovery_engine",
                "feed_action": action,
                "feed_state_version": "feed-state",
                "feed_version": state.active_version,
                "preparation_state": "build_failed" if build_failed else "preparing",
                "prepared_candidate_available": False,
                "artifact_status": "build_failed" if build_failed else "preparing",
                "artifact_quality": "build_failed" if build_failed else "preparing",
                "refresh_outcome": action,
                "preparation_reasons": failure_reasons,
            },
            candidate_pool_counts={},
            provider_timings_ms={},
            home_tab_lanes={},
            accepted=False,
            quality_reasons=[
                (
                    "initial_feed_build_failed"
                    if build_failed
                    else "initial_feed_not_ready"
                )
            ],
            artifact_source=action,
        )
        return home_response_from_artifact(
            artifact,
            request_id=request_id,
            page_size=page_size,
        )

    def _schedule_queue_recovery_once(
        self,
        req: Any,
        taste: Any,
        state: FeedState,
        *,
        outcome: str,
        reason: str,
        inventory_generation: str = "",
    ) -> bool:
        parent = (state.ready_feeds[-1].session_id if state.ready_feeds else "") or (
            state.active_feed.session_id if state.active_feed is not None else ""
        )
        key = f"{outcome}:{parent}:{inventory_generation}"
        if key in set(state.recovery_attempt_keys or []):
            return False
        state.recovery_attempt_keys = [*(state.recovery_attempt_keys or []), key][-16:]
        save_feed_state(self._server, state)
        self._schedule_preparation(req, taste, reason=reason)
        return True

    def _schedule_release_metadata_enrichment(self, req: Any, taste: Any) -> None:
        """Hydrate persisted inventory metadata without claiming feed prep."""
        key = f"release-metadata:{taste.user_scope_id}"
        with self._background_build_lock:
            if key in self._signal_reconciliations_inflight:
                return
            self._signal_reconciliations_inflight.add(key)

        def enrich() -> None:
            started = time.perf_counter()
            try:
                inventory = load_candidate_inventory(
                    self._server,
                    taste.user_scope_id,
                    profile_fingerprint=taste.profile_key,
                    require_fresh=False,
                    allow_profile_mismatch=True,
                )
                if inventory is None or not inventory.is_ready:
                    return
                enriched = complete_inventory_release_metadata(self._server, inventory)
                if enriched != inventory:
                    store_candidate_inventory(
                        self._server,
                        enriched,
                        expected_ready_generation_id=inventory.generation_id,
                    )
                print(
                    f"[EBB:release-metadata] scope={taste.user_scope_id} "
                    f"outcome=stored elapsed_ms={int((time.perf_counter()-started)*1000)}",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"[EBB:release-metadata] scope={taste.user_scope_id} "
                    f"outcome=error error={type(exc).__name__}", flush=True,
                )
            finally:
                with self._background_build_lock:
                    self._signal_reconciliations_inflight.discard(key)

        try:
            # Metadata hydration must never occupy the two feed-composition
            # workers while the ready queue is being replenished.
            self._metadata_executor.submit(enrich)
        except Exception:
            with self._background_build_lock:
                self._signal_reconciliations_inflight.discard(key)

    def _schedule_radio_reservoir_expansion(
        self,
        req: Any,
        taste: Any,
        inventory: Any,
    ) -> bool:
        """Expand qualified radio supply off the queue-critical path.

        This job only updates persisted candidate/radio inventories. It never
        changes feed state, promotes a feed, or schedules another preparation.
        """
        diagnostics = dict(getattr(inventory, "diagnostics", {}) or {})
        reservoir_size = int(diagnostics.get("reservoir_size") or 0)
        discovery_deficit = int(diagnostics.get("discovery_deficit") or 0)
        if reservoir_size >= RADIO_RESERVOIR_TARGET_CARDS and discovery_deficit <= 0:
            return False
        key = f"radio-reservoir:{taste.user_scope_id}:{inventory.generation_id}"
        with self._background_build_lock:
            if key in self._radio_reservoir_inflight:
                return False
            self._radio_reservoir_inflight.add(key)

        def expand() -> None:
            schedule_continuation = False
            continuation_inventory = None
            try:
                current = load_candidate_inventory(
                    self._server,
                    taste.user_scope_id,
                    profile_fingerprint=taste.profile_key,
                    require_fresh=False,
                    allow_profile_mismatch=True,
                )
                if current is None:
                    return
                radio_progress = dict(
                    (load_artist_radio_inventory(
                        self._server,
                        taste.user_scope_id,
                        profile_fingerprint=taste.profile_key,
                    ).diagnostics or {}).get("radio_expansion_progress")
                    or {}
                )
                progress_base_revision = int(radio_progress.get("progress_revision") or 0)
                radio_ledger = dict(current.acquisition_ledger or {})
                for source, target in (
                    ("anchor_cursor_next", "anchor_cursor"),
                    ("artist_cursor_next", "artist_cursor"),
                    ("radio_discovery_cursor_next", "radio_discovery_cursor"),
                    ("request_progress", "request_progress"),
                ):
                    if radio_progress.get(source) is not None:
                        radio_ledger[target] = radio_progress[source]
                # Build discovery supply from persisted artist evidence before
                # repairing familiar radio anchors. This remains local-only;
                # the catalog requests below are the existing bounded worker.
                radio_state = load_artist_radio_inventory(
                    self._server,
                    taste.user_scope_id,
                    profile_fingerprint=taste.profile_key,
                )
                familiar = {
                    str(name).casefold()
                    for name in (
                        *getattr(taste, "listened_artists", []),
                        *getattr(taste, "top_artists", []),
                        *getattr(taste, "artist_hints", []),
                    )
                }
                suppressed = {str(value).casefold() for value in (getattr(taste, "avoid_ids", []) or [])}
                # Honor the same durable strong artist-cluster negatives used
                # by radio composition, including canonical IDs and names.
                feedback = dict((getattr(taste, "source_profile", {}) or {}).get("negative_feedback") or {})
                if isinstance(feedback.get("by_type"), dict):
                    feedback = dict(feedback.get("by_type") or {})
                for value, strength in dict(feedback.get("artist_cluster") or {}).items():
                    try:
                        if float(strength or 0.0) >= 0.85:
                            suppressed.add(str(value).casefold())
                    except (TypeError, ValueError):
                        continue
                occupied = {
                    str(card.get("seed_artist_key") or card.get("artist_name") or "").casefold()
                    for card in [
                        *(getattr(radio_state, "cards", []) or []),
                        *(getattr(radio_state, "reservoir_cards", []) or []),
                    ]
                    if isinstance(card, dict)
                }
                # Active and ready queued feed artifacts are also reserved;
                # discovery must not enqueue a seed already visible or ready.
                feed_state = load_feed_state(self._server, taste.user_scope_id)
                for artifact in [
                    getattr(feed_state, "active_feed", None),
                    getattr(feed_state, "prepared_feed", None),
                    *list(getattr(feed_state, "ready_feeds", []) or []),
                ]:
                    artifact = getattr(artifact, "artifact", artifact)
                    for row in getattr(artifact, "rows", []) or []:
                        if getattr(row, "kind", "") != "popular_radio":
                            continue
                        for card in getattr(row, "items", []) or []:
                            if isinstance(card, dict):
                                occupied.add(
                                    str(card.get("seed_artist_key") or card.get("artist_name") or "").casefold()
                                )
                discovery_seeds = []
                seed_sources = {}
                attempted = {
                    str(value).casefold()
                    for value in (radio_progress.get("attempted_discovery_seeds") or [])
                }
                for source_name in ("artist_graph", "popularity", "discovery_universe"):
                    for candidate in current.pools.get(source_name, []) or []:
                        item = getattr(candidate, "item", candidate)
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("artist") or item.get("artist_name") or "").strip()
                        if not name or name.casefold() in familiar or name.casefold() in occupied:
                            continue
                        identity = str(
                            item.get("musicbrainz_artist_id")
                            or item.get("provider_artist_id")
                            or item.get("artist_id")
                            or ""
                        ).strip()
                        relationship_evidence = any(
                            str(item.get(field) or "").strip()
                            for field in (
                                "related_to_artist", "artist_graph", "relationship_evidence",
                                "relationship_provenance", "radio_catalog_role", "source",
                            )
                        )
                        if not identity and (source_name != "artist_graph" or not relationship_evidence):
                            continue
                        if identity.casefold() in suppressed or name.casefold() in suppressed:
                            continue
                        seed_key = (identity or name).casefold()
                        if seed_key in attempted or name.casefold() in attempted:
                            continue
                        if any(str(seed.get("key")).casefold() == seed_key for seed in discovery_seeds):
                            continue
                        discovery_seeds.append({
                            "key": seed_key,
                            "name": name,
                            "musicbrainz_artist_id": item.get("musicbrainz_artist_id"),
                            "provider_artist_id": item.get("provider_artist_id"),
                            "source": source_name,
                        })
                        seed_sources[source_name] = int(seed_sources.get(source_name, 0)) + 1
                        if len(discovery_seeds) >= 8:
                            break
                    if len(discovery_seeds) >= 8:
                        break
                discovery_deficit = int((getattr(radio_state, "diagnostics", {}) or {}).get("discovery_deficit") or 0)
                plan = build_enrichment_plan(
                    taste,
                    acquisition_ledger=radio_ledger,
                    allowed_pools={"radio_artist_catalog"},
                    radio_discovery_artist_seeds=discovery_seeds,
                    radio_discovery_deficit=discovery_deficit,
                )
                supply = materialize_enrichment_plan(
                    self._server,
                    plan,
                    time_budget_seconds=6.0,
                    max_workers=3,
                    max_pending_jobs=3,
                )
                supply = verify_materialized_supply(
                    self._server,
                    supply,
                    taste,
                    max_new_verifications=8,
                    max_workers=3,
                )
                enriched = build_candidate_inventory(
                    self._server,
                    taste,
                    previous=current,
                    materialized_supply=supply,
                )
                built = build_artist_radio_inventory(
                    taste,
                    enriched.pools,
                    server=self._server,
                )
                progress = dict(getattr(supply, "diagnostics", {}) or {})
                built.diagnostics["radio_expansion_progress"] = {
                    key: progress.get(key)
                    for key in (
                        "anchor_cursor_next",
                    "artist_cursor_next",
                    "radio_discovery_cursor_next",
                        "request_progress",
                        "completed_request_count",
                    )
                    if progress.get(key) is not None
                }
                built.diagnostics["radio_expansion_progress"]["discovery_seed_candidates_by_source"] = seed_sources
                built.diagnostics["radio_expansion_progress"]["discovery_deficit"] = discovery_deficit
                built.diagnostics["radio_expansion_progress"]["attempted_discovery_seeds"] = [
                    *list(radio_progress.get("attempted_discovery_seeds") or []),
                    *[
                        request.metadata.get("radio_seed_key") or request.metadata.get("radio_seed_artist")
                    for request in plan.requests
                    if request.kind == "canonical_artist_radio_catalog"
                    and request.metadata.get("radio_seed_artist") in {
                        seed.get("name") for seed in discovery_seeds
                    }
                    ],
                ]
                built.diagnostics["radio_expansion_progress"]["progress_base_revision"] = progress_base_revision
                built.diagnostics["radio_expansion_progress"]["progress_revision"] = progress_base_revision + 1
                prior_no_progress = int(radio_progress.get("no_progress_cycles") or 0)
                before_reservoir = int((getattr(radio_state, "diagnostics", {}) or {}).get("reservoir_size") or 0)
                before_discovery = int((getattr(radio_state, "diagnostics", {}) or {}).get("discovery_card_count") or 0)
                # First merge only the qualified supply and cursor progress.
                # Progress is finalized from the durable post-merge state
                # below; never infer success from provider completion alone.
                merge_store_artist_radio_inventory(self._server, built)
                if getattr(built, "artwork_repair_records", None):
                    self._schedule_radio_artwork_repairs(
                        req, taste, list(built.artwork_repair_records)
                    )
                merged_state = load_artist_radio_inventory(
                    self._server, taste.user_scope_id,
                    profile_fingerprint=taste.profile_key,
                )
                merged_diag = dict(getattr(merged_state, "diagnostics", {}) or {})
                made_progress = (
                    int(merged_diag.get("reservoir_size") or 0) > before_reservoir
                    or int(merged_diag.get("discovery_card_count") or 0) > before_discovery
                )
                no_progress_cycles = 0 if made_progress else prior_no_progress + 1
                progress_final = dict(
                    (getattr(merged_state, "diagnostics", {}) or {}).get("radio_expansion_progress")
                    or getattr(built, "diagnostics", {}).get("radio_expansion_progress", {})
                    or {}
                )
                progress_final["no_progress_cycles"] = no_progress_cycles
                progress_final["exhausted"] = bool(
                    not plan.requests or no_progress_cycles >= 2
                )
                # The first merge advanced the durable revision. Rebase this
                # metadata-only correction to that revision so the CAS merge
                # accepts no-progress/exhaustion instead of treating it as a
                # stale worker completion.
                progress_final["progress_base_revision"] = int(
                    progress_final.get("progress_revision") or progress_base_revision
                )
                # Persist the corrected durable progress without replacing the
                # reservoir selected by a concurrent rotation.
                built.diagnostics["radio_expansion_progress"] = progress_final
                merge_store_artist_radio_inventory(self._server, built)
                final_state = load_artist_radio_inventory(
                    self._server, taste.user_scope_id,
                    profile_fingerprint=taste.profile_key,
                )
                final_diag = dict(getattr(final_state, "diagnostics", {}) or {})
                schedule_continuation = bool(
                    not bool(progress_final.get("exhausted"))
                    and (
                        int(final_diag.get("reservoir_size") or 0) < RADIO_RESERVOIR_TARGET_CARDS
                        or int(final_diag.get("discovery_deficit") or 0) > 0
                    )
                )
                continuation_inventory = final_state
            except Exception:
                # Enrichment is opportunistic; a provider failure must not
                # affect the already-published queue or its state machine.
                return
            finally:
                with self._background_build_lock:
                    self._radio_reservoir_inflight.discard(key)
                # Clear the in-flight key before enqueueing the next bounded
                # cycle; this prevents recursive scheduling from being
                # suppressed by the guard while preserving serialization.
                if schedule_continuation and continuation_inventory is not None:
                    self._schedule_radio_reservoir_expansion(
                        req, taste, continuation_inventory
                    )

        try:
            self._radio_reservoir_executor.submit(expand)
        except Exception:
            with self._background_build_lock:
                self._radio_reservoir_inflight.discard(key)
            return False
        return True

    def _schedule_radio_artwork_repairs(
        self,
        req: Any,
        taste: Any,
        records: list[Dict[str, Any]],
    ) -> int:
        """Dispatch unique repairs off the feed-composition critical path."""
        scheduled: list[tuple[str, Dict[str, Any]]] = []
        with self._background_build_lock:
            for record in records[:16]:
                identity = str(
                    record.get("canonical_artist_id")
                    or record.get("provider_artist_id")
                    or record.get("id")
                    or record.get("name")
                    or ""
                ).strip().casefold()
                if not identity:
                    continue
                key = f"radio-artwork-repair:{taste.user_scope_id}:{identity}"
                if key in self._signal_reconciliations_inflight:
                    continue
                self._signal_reconciliations_inflight.add(key)
                scheduled.append((key, dict(record)))
        if not scheduled:
            return 0

        def dispatch() -> None:
            # A repair batch can complete several artist records in quick
            # succession.  Coalesce those callbacks into one local radio
            # recomposition; each callback must not start a full feed build.
            reconcile_scheduled = False
            for key, record in scheduled:
                callback_called = False

                def completed(updated: Dict[str, Any], *, repair_key: str = key) -> None:
                    nonlocal callback_called, reconcile_scheduled
                    callback_called = True
                    name = str(updated.get("name") or "").strip()
                    if name and str(updated.get("thumbnail") or "").startswith(
                        "/artist_artwork/"
                    ):
                        remember_catalog_entity(
                            self._server,
                            user_scope_id="global",
                            query=name,
                            entity_type="artist",
                            item=updated,
                            confidence=0.98,
                            event_weight=0.0,
                            event_type="artist_metadata",
                            source="popular_radio_artwork",
                            learn_query_alias=False,
                        )
                        if not reconcile_scheduled:
                            reconcile_scheduled = True
                            self._schedule_preparation_after_response(
                                req,
                                reason="radio_artwork_reconcile",
                                dedupe_key=f"radio-artwork:{taste.user_scope_id}",
                            )
                    with self._background_build_lock:
                        self._signal_reconciliations_inflight.discard(repair_key)

                accepted = schedule_artist_artwork_cache(
                    self._server,
                    record,
                    on_cached=completed,
                )
                if not accepted and not callback_called:
                    with self._background_build_lock:
                        self._signal_reconciliations_inflight.discard(key)

        try:
            self._radio_artwork_executor.submit(dispatch)
        except Exception:
            with self._background_build_lock:
                for key, _record in scheduled:
                    self._signal_reconciliations_inflight.discard(key)
            return 0
        return len(scheduled)

    def _schedule_preparation(self, req: Any, taste: Any, *, reason: str) -> None:
        # Release metadata is an inventory concern, not a feed rotation. Keep
        # it off the serialized feed-builder lease so a full ready queue is
        # never marked preparing (or rebuilt) merely to hydrate years/dates.
        if reason == "release_metadata_replenish":
            self._schedule_release_metadata_enrichment(req, taste)
            return
        current = load_feed_state(self._server, taste.user_scope_id)
        if current is not None and len(current.ready_feeds or []) >= 2:
            print(
                f"[EBB:feed-queue] prepare scope={taste.user_scope_id} "
                f"outcome=noop_full_queue reason={reason}", flush=True
            )
            return
        fingerprint = self._background_fingerprint(taste)
        refresh_token = str(getattr(req, "refresh_token", "") or "").strip()
        urgent = reason in {
            "pull_to_refresh",
            "search_session_intent",
            "post_refresh",
            "post_promotion",
            "post_initial_active",
            "queue_replenish",
            "inventory_replenish",
            "release_metadata_replenish",
            "radio_artwork_ready",
            "radio_artwork_reconcile",
            "radio_catalog_replenish",
            "popular_radio_inventory_shortage",
            # These are persisted recovery reasons. Their callers are already
            # deduplicated by the per-profile in-flight claim, so cooldown
            # must not strand a due retry after its one-shot timer fires.
            "initial_feed",
            "background_prepare",
            "launch_missing_successor",
            "queue_novelty_delta",
            "targeted_row_repair",
            "queue_store_retry",
        }
        if not self._claim_background_build(
            fingerprint,
            urgent=urgent,
            refresh_token=refresh_token,
        ):
            if urgent:
                with self._background_build_lock:
                    if fingerprint in self._background_builds_inflight:
                        inflight_token = self._background_build_tokens.get(
                            fingerprint, ""
                        )
                        # Polling the same explicit refresh observes the
                        # running build; it is not a request for another one.
                        if not refresh_token or refresh_token != inflight_token:
                            self._pending_background_builds[fingerprint] = (
                                req,
                                taste,
                                reason,
                            )
            return
        building_state = load_feed_state(self._server, taste.user_scope_id)
        if building_state is None:
            building_state = FeedState(user_scope_id=taste.user_scope_id)
        # State may have changed while the claim was being acquired (for
        # example a promotion or another worker storing Feed B). Recheck after
        # reload and release the claim without touching preparation markers.
        if len(building_state.ready_feeds or []) >= 2:
            self._release_background_build(fingerprint)
            print(
                f"[EBB:feed-queue] prepare scope={taste.user_scope_id} "
                f"outcome=noop_full_queue_after_claim reason={reason}", flush=True
            )
            return
        if building_state.active_feed is None:
            building_state.generation_status = "inventory_building"
            save_feed_state(self._server, building_state)
        lease_started = time.time()
        lease_deadline = lease_started + 120.0
        lease_token = uuid.uuid4().hex
        building_state.preparation_lease_started_at = lease_started
        building_state.preparation_lease_deadline = lease_deadline
        building_state.preparation_lease_reason = reason
        building_state.preparation_lease_token = lease_token
        building_state.retry_at = 0.0
        building_state.retry_reason = ""
        if building_state.active_feed is not None:
            building_state.generation_status = "preparing"
        save_feed_state(self._server, building_state)

        def prepare() -> None:
            prepare_started = time.perf_counter()
            phase_timings: Dict[str, int] = {}
            try:
                def lease_current() -> bool:
                    current = load_feed_state(
                        self._server, taste.user_scope_id
                    )
                    return bool(
                        current is not None
                        and str(current.preparation_lease_token or "")
                        == lease_token
                    )

                def within_lease_budget() -> bool:
                    return time.time() < (lease_deadline - 1.0) and lease_current()

                phase_started = time.perf_counter()
                previous_inventory = load_candidate_inventory(
                    self._server,
                    taste.user_scope_id,
                    profile_fingerprint=taste.profile_key,
                    require_fresh=False,
                    # Candidate supply is user-scoped and can be re-ranked
                    # against a newer taste fingerprint. A history update
                    # must not discard the full persisted provider inventory.
                    allow_profile_mismatch=True,
                )
                if previous_inventory is not None:
                    local_candidate_path = bool(
                        previous_inventory.is_ready
                        and reason
                        in {
                            "post_initial_active",
                            "post_promotion",
                            "queue_replenish",
                            "launch_missing_successor",
                            "background_prepare",
                            "radio_artwork_ready",
                            "radio_artwork_reconcile",
                            "post_launch_promotion",
                            "pull_to_refresh",
                            "queue_novelty_delta",
                            "targeted_row_repair",
                            "queue_store_retry",
                        }
                    )
                    if (
                        previous_inventory.profile_fingerprint != taste.profile_key
                        and not local_candidate_path
                    ):
                        ledger = dict(previous_inventory.acquisition_ledger or {})
                        ledger["reused_for_profile_fingerprint"] = {
                            "from": previous_inventory.profile_fingerprint,
                            "to": taste.profile_key,
                            "at": time.time(),
                        }
                        previous_inventory = replace(
                            previous_inventory,
                            profile_fingerprint=taste.profile_key,
                            acquisition_ledger=ledger,
                        )
                    refreshed_previous = (
                        previous_inventory
                        if local_candidate_path
                        else refresh_candidate_inventory_coverage(
                            previous_inventory,
                            taste=taste,
                        )
                    )
                    if (
                        refreshed_previous.acquisition_ledger
                        != previous_inventory.acquisition_ledger
                    ):
                        if not lease_current():
                            return
                        store_candidate_inventory(
                            self._server,
                            refreshed_previous,
                            expected_ready_generation_id=previous_inventory.generation_id,
                        )
                    previous_inventory = refreshed_previous
                if reason == "radio_artwork_reconcile" and previous_inventory is None:
                    # Artwork repair may only reconcile persisted local supply;
                    # it must never fan out provider acquisition by itself.
                    if lease_current():
                        terminal = load_feed_state(
                            self._server, taste.user_scope_id
                        ) or building_state
                        terminal.generation_status = (
                            "ready" if terminal.active_feed is not None else "delayed"
                        )
                        terminal.preparation_lease_started_at = 0.0
                        terminal.preparation_lease_deadline = 0.0
                        terminal.preparation_lease_reason = ""
                        terminal.preparation_lease_token = ""
                        terminal.retry_at = 0.0
                        terminal.retry_reason = ""
                        terminal.dirty_reasons = ["radio_inventory_needed"]
                        save_feed_state(
                            self._server,
                            terminal,
                            expected_preparation_lease_token=lease_token,
                        )
                    return
                phase_timings["inventory_load_refresh_ms"] = int((time.perf_counter() - phase_started) * 1000)
                compose_from_ready_inventory = bool(
                    previous_inventory is not None
                    and previous_inventory.is_ready
                    and reason
                    in {
                        "post_initial_active",
                        "post_promotion",
                        "queue_replenish",
                        "launch_missing_successor",
                        "background_prepare",
                        "radio_artwork_ready",
                        "radio_artwork_reconcile",
                        "post_launch_promotion",
                        "pull_to_refresh",
                        "queue_novelty_delta",
                        "targeted_row_repair",
                        "queue_store_retry",
                    }
                )
                # A ready persisted inventory + reservoir is the queue-critical
                # local path. Radio selection is injected into a transient copy
                # for artifact composition only; the main candidate inventory
                # remains owned by acquisition/enrichment.
                local_only_path = bool(compose_from_ready_inventory)
                queue_depth = len(building_state.ready_feeds or [])
                # Compose from persisted ready inventory first.  A queued
                # successor must not trigger remote novelty work merely
                # because it is Feed B; bounded novelty is reserved for a
                # later retry when local composition cannot produce a valid
                # distinct artifact.
                novelty_delta_requested = reason == "queue_novelty_delta"
                targeted_radio_work = reason in {
                    "popular_radio_inventory_shortage",
                    "radio_catalog_replenish",
                }
                catalog_result = (
                    {"reason": "bounded_novelty_delta_pending"}
                    if novelty_delta_requested
                    else {"reason": "targeted_radio_catalog_pending"}
                    if targeted_radio_work
                    else {"reason": "ready_inventory_queue_composition"}
                    if compose_from_ready_inventory
                    else schedule_catalog_population(
                        self._server,
                        user_scope_id=taste.user_scope_id,
                        req=req,
                        taste=taste,
                        reason=f"feed_inventory_{reason}",
                        min_interval_seconds=300.0,
                        wait_for_completion=False,
                        wait_timeout_seconds=0.0,
                    )
                )
                ready_inventory = previous_inventory
                working_inventory = previous_inventory
                active_optional_rows = (
                    _artifact_row_kinds(building_state.active_feed)
                    & _OPTIONAL_ROW_KINDS
                )
                persisted_optional_rows = _completed_optional_rows(
                    previous_inventory.coverage
                    if previous_inventory is not None
                    else {}
                )
                # A completed optional shelf may already be waiting in the
                # persisted inventory from an interrupted worker. Build its
                # prepared feed immediately instead of making it fetch again.
                prepared_new_inventory = bool(
                    previous_inventory is not None
                    and previous_inventory.is_ready
                    and not compose_from_ready_inventory
                    and (persisted_optional_rows - active_optional_rows)
                )
                materialized_supply = None
                radio_inventory_evaluated = False
                radio_inventory_ready = False
                radio_artwork_repairs_pending = 0
                max_cycles = (
                    6
                    if building_state.active_feed is not None
                    else max(
                        2,
                        min(6, ((len(taste.full_history_tracks) + 23) // 24) + 1),
                    )
                )
                cycle_budget = (
                    1
                    if compose_from_ready_inventory or novelty_delta_requested
                    else max_cycles
                )
                for _cycle in range(0 if prepared_new_inventory else cycle_budget):
                    if not within_lease_budget():
                        return
                    inventory_build_started = time.perf_counter()
                    if compose_from_ready_inventory and not novelty_delta_requested:
                        candidate_inventory = working_inventory
                        if candidate_inventory is None:
                            break
                    else:
                        enrichment_plan = build_enrichment_plan(
                            taste,
                            acquisition_ledger=(
                                {
                                    **(
                                        working_inventory.acquisition_ledger
                                        if working_inventory is not None
                                        else {}
                                    ),
                                    "radio_seed_counts": {
                                        str(name).casefold(): sum(
                                            1
                                            for values in (
                                                working_inventory.pools.values()
                                                if working_inventory is not None
                                                else []
                                            )
                                            for candidate in values or []
                                            if getattr(candidate, "item_type", "track") == "track"
                                            and str((candidate.item or {}).get("artist") or "").casefold() == str(name).casefold()
                                        )
                                        for name in (taste.top_artists or taste.artist_hints or [])
                                    },
                                }
                            ),
                            allowed_pools=(
                                {"radio_artist_catalog"}
                                if targeted_radio_work
                                else None
                            ),
                        )
                        materialized_supply = materialize_enrichment_plan(
                            self._server,
                            enrichment_plan,
                            time_budget_seconds=(6.0 if targeted_radio_work else None),
                            max_workers=(3 if targeted_radio_work else 6),
                            max_pending_jobs=(3 if targeted_radio_work else 6),
                        )
                        materialized_supply = verify_materialized_supply(
                            self._server,
                            materialized_supply,
                            taste,
                            max_new_verifications=(
                                8
                                if targeted_radio_work
                                else 16
                                if novelty_delta_requested
                                else None
                            ),
                            max_workers=4,
                        )
                        if not within_lease_budget():
                            return
                        candidate_inventory = build_candidate_inventory(
                            self._server,
                            taste,
                            previous=working_inventory,
                            materialized_supply=materialized_supply,
                        )
                        candidate_inventory = complete_inventory_release_metadata(
                            self._server,
                            candidate_inventory,
                            allow_remote_lookup=not targeted_radio_work,
                        )
                        if not within_lease_budget():
                            return
                    phase_timings["profile_inventory_build_ms"] = (
                        phase_timings.get("profile_inventory_build_ms", 0)
                        + int(
                            (time.perf_counter() - inventory_build_started) * 1000
                        )
                    )
                    # Use the same final successor epoch that will be stored
                    # for this artifact.  This keeps Feed A/B radio rotations
                    # distinct when both are prepared before promotion.
                    epoch_state = (
                        load_feed_state(self._server, taste.user_scope_id)
                        or building_state
                    )
                    queue_depth = len(epoch_state.ready_feeds or [])
                    successor_epoch = epoch_state.rotation_epoch + queue_depth + 1
                    radio_taste = self._rotation_taste(
                        taste,
                        epoch_state.active_feed,
                        reason=reason,
                        rotation_epoch=successor_epoch,
                        inventory_generation=candidate_inventory.generation_id,
                    )
                    radio_started = time.perf_counter()
                    # Derive used identities from the active artifact and the
                    # whole ready chain. A successor must consume a different
                    # reservoir slice rather than shuffle the same row.
                    used_card_ids: set[str] = set()
                    used_track_ids: set[str] = set()
                    for prior in [
                        epoch_state.active_feed,
                        *[entry.artifact for entry in epoch_state.ready_feeds or []],
                    ]:
                        for row in (prior.rows if prior is not None else []):
                            if row.kind != "popular_radio":
                                continue
                            for card in row.items or []:
                                if not isinstance(card, dict):
                                    continue
                                card_id = str(card.get("id") or "").strip()
                                if card_id:
                                    used_card_ids.add(card_id)
                                for track in card.get("tracks") or card.get("items") or []:
                                    if not isinstance(track, dict):
                                        continue
                                    track_id = canonical_item_identity(
                                        track,
                                        item_type="track",
                                    )
                                    if track_id:
                                        used_track_ids.add(track_id)
                    persisted_radio_inventory = load_artist_radio_inventory(
                        self._server,
                        taste.user_scope_id,
                        profile_fingerprint=taste.profile_key,
                    )
                    built_radio_inventory = None
                    radio_inventory_built = False
                    selected_radio = select_radio_rotation(
                        persisted_radio_inventory,
                        excluded_card_ids=used_card_ids,
                        excluded_track_ids=used_track_ids,
                        epoch=successor_epoch,
                    )
                    if selected_radio is not None and selected_radio.is_ready:
                        radio_inventory = selected_radio
                        radio_inventory.diagnostics.update(
                            {
                                "persisted_inventory_reused": True,
                                "composition_path": "persisted_reservoir_slice",
                                # Do not carry stale repair counts from an
                                # older inventory into the queue contract.
                                "artwork_repair_scheduled_count": 0,
                                "artwork_repair_pending_count": 0,
                                "artwork_repair_dispatched_count": 0,
                            }
                        )
                    else:
                        built_radio_inventory = build_artist_radio_inventory(
                            radio_taste,
                            candidate_inventory.pools,
                            server=self._server,
                        )
                        radio_inventory_built = True
                        merged_radio_inventory = merge_radio_reservoirs(
                            persisted_radio_inventory,
                            built_radio_inventory,
                        )
                        radio_inventory = select_radio_rotation(
                            merged_radio_inventory,
                            excluded_card_ids=used_card_ids,
                            excluded_track_ids=used_track_ids,
                            epoch=successor_epoch,
                        ) or merged_radio_inventory
                        if local_only_path and merged_radio_inventory.is_ready:
                            merge_store_artist_radio_inventory(
                                self._server,
                                merged_radio_inventory,
                            )
                        dispatched_artwork_repairs = 0
                        if not local_only_path:
                            dispatched_artwork_repairs = self._schedule_radio_artwork_repairs(
                                req,
                                taste,
                                list(built_radio_inventory.artwork_repair_records or []),
                            )
                        radio_inventory.diagnostics.update(
                            {
                                "persisted_inventory_reused": False,
                                "composition_path": (
                                    "local_reservoir_refresh"
                                    if compose_from_ready_inventory
                                    else "initial_reservoir_build"
                                ),
                                "artwork_repair_dispatched_count": (
                                    dispatched_artwork_repairs
                                ),
                            }
                        )
                    phase_timings["radio_composition_ms"] = (
                        phase_timings.get("radio_composition_ms", 0)
                        + int((time.perf_counter() - radio_started) * 1000)
                    )
                    radio_inventory.diagnostics.update(
                        {
                            "successor_rotation_epoch": successor_epoch,
                            "generation_id": radio_inventory.generation_id,
                            "inventory_age_seconds": max(
                                int(time.time() - radio_inventory.generated_at),
                                0,
                            ),
                            "persisted_inventory_reused": not radio_inventory_built,
                        }
                    )
                    if not local_only_path:
                        if not lease_current():
                            return
                        store_artist_radio_inventory(self._server, radio_inventory)
                    radio_inventory_evaluated = True
                    radio_inventory_ready = radio_inventory.is_ready
                    radio_artwork_repairs_pending = int(
                        radio_inventory.diagnostics.get(
                            "artwork_repair_dispatched_count"
                        )
                        or 0
                    )
                    candidate_pools = {
                        name: list(values or [])
                        for name, values in candidate_inventory.pools.items()
                    }
                    published_radio_cards = radio_card_candidates(radio_inventory)
                    candidate_pools["popular_radio_cards"] = published_radio_cards
                    candidate_counts = dict(candidate_inventory.candidate_counts or {})
                    candidate_counts["popular_radio_cards"] = len(
                        published_radio_cards
                    )
                    coverage = candidate_inventory_coverage(
                        candidate_pools,
                        taste=taste,
                    )
                    candidate_counts["coverage_unique_tracks"] = int(
                        (coverage.get("actual") or {}).get("unique_tracks") or 0
                    )
                    candidate_counts["coverage_ready"] = (
                        1 if coverage.get("ready") is True else 0
                    )
                    artifact_inventory = replace(
                        candidate_inventory,
                        pools=candidate_pools,
                        candidate_counts=candidate_counts,
                        coverage=coverage,
                        row_coverage={
                            "ready": coverage.get("ready") is True,
                            "actual": dict(coverage.get("actual") or {}),
                            "minimums": dict(coverage.get("minimums") or {}),
                            "failed_contracts": list(
                                coverage.get("failed_contracts") or []
                            ),
                        },
                    )
                    if local_only_path:
                        # Do not rewrite persisted candidate inventory merely to
                        # carry the selected radio rotation into this artifact.
                        candidate_inventory = artifact_inventory
                    else:
                        candidate_inventory = artifact_inventory
                        working_inventory = candidate_inventory
                    if not candidate_inventory.is_ready:
                        continue
                    expected_generation = (
                        ready_inventory.generation_id
                        if ready_inventory is not None
                        else ""
                    )
                    if not lease_current():
                        print(
                            f"[EBB:feed-prepare] scope={taste.user_scope_id} "
                            "outcome=lease_superseded before=inventory_store",
                            flush=True,
                        )
                        return
                    stored_inventory = True
                    if not local_only_path:
                        stored_inventory = store_candidate_inventory(
                            self._server,
                            candidate_inventory,
                            expected_ready_generation_id=expected_generation,
                        )
                    if not stored_inventory:
                        current = load_feed_state(self._server, taste.user_scope_id)
                        if current is None:
                            current = FeedState(user_scope_id=taste.user_scope_id)
                        if not lease_current():
                            return
                        mark_feed_dirty(
                            self._server,
                            current,
                            "inventory_generation_superseded",
                        )
                        return
                    ready_inventory = candidate_inventory
                    prepared_new_inventory = True
                    if ready_inventory.is_ready:
                        completed_optional_rows = _completed_optional_rows(coverage)
                        newly_completed_rows = (
                            completed_optional_rows - active_optional_rows
                        )
                        missing_optional_rows = (
                            _OPTIONAL_ROW_KINDS
                            - active_optional_rows
                            - completed_optional_rows
                        )
                        if building_state.active_feed is not None:
                            # Publish any newly completed shelf now. The next
                            # prepared successor continues the other shortages.
                            if newly_completed_rows:
                                break
                            if missing_optional_rows and _cycle + 1 < max_cycles:
                                continue
                        break
                if (
                    building_state.active_feed is not None
                    and radio_inventory_evaluated
                    and not radio_inventory_ready
                ):
                    if not lease_current():
                        return
                    current = load_feed_state(
                        self._server,
                        taste.user_scope_id,
                    ) or FeedState(user_scope_id=taste.user_scope_id)
                    current.generation_status = "preparing"
                    current.dirty_reasons = [
                        (
                            f"popular_radio_artwork_pending:"
                            f"{radio_artwork_repairs_pending}"
                            if radio_artwork_repairs_pending > 0
                            else "popular_radio_inventory_shortage"
                        )
                    ]
                    save_feed_state(
                        self._server,
                        current,
                        expected_preparation_lease_token=lease_token,
                    )
                    if reason != "queue_novelty_delta":
                        # A radio shortage is repaired only by bounded
                        # radio-catalog enrichment; never escalate to broad
                        # novelty acquisition from ordinary queue builds.
                        retry_reason = "radio_catalog_replenish"
                        retry_at = time.time() + 5.0
                        current.generation_status = "delayed"
                        current.retry_at = retry_at
                        current.retry_reason = retry_reason
                        current.preparation_lease_started_at = 0.0
                        current.preparation_lease_deadline = 0.0
                        current.preparation_lease_reason = ""
                        current.preparation_lease_token = ""
                        save_feed_state(
                            self._server,
                            current,
                            expected_preparation_lease_token=lease_token,
                        )
                        timer = threading.Timer(
                            5.0,
                            lambda: self._schedule_preparation(
                                req,
                                taste,
                                reason=retry_reason,
                            ),
                        )
                        timer.daemon = True
                        timer.start()
                    return
                if not prepared_new_inventory:
                    # Provider jobs have already persisted every successful
                    # result.  An incomplete pass is still building inventory;
                    # it must not become build_failed and it must not rotate a
                    # previous feed using an older candidate generation.
                    current = load_feed_state(self._server, taste.user_scope_id)
                    if current is None:
                        current = FeedState(user_scope_id=taste.user_scope_id)
                    current.generation_status = "inventory_building"
                    shortages = list(
                        (working_inventory.coverage if working_inventory else {}).get(
                            "failed_contracts"
                        )
                        or []
                    )
                    current.dirty_reasons = (
                        [
                            "inventory_shortage:"
                            + ",".join(str(value) for value in shortages)
                        ]
                        if shortages
                        else ["inventory_acquisition_pending"]
                    )
                    save_feed_state(
                        self._server,
                        current,
                        expected_preparation_lease_token=lease_token,
                    )
                    return
                if ready_inventory is None:
                    return
                intent_payload = load_inventory_intent_delta(
                    self._server,
                    taste.user_scope_id,
                )
                inventory = (
                    apply_inventory_intent_delta(ready_inventory, intent_payload)
                    if intent_payload
                    else ready_inventory
                )
                current = load_feed_state(self._server, taste.user_scope_id)
                if current is None:
                    current = FeedState(user_scope_id=taste.user_scope_id)
                base_active_version = current.active_version
                base_inventory_generation = current.active_inventory_generation
                queued_artifacts = [
                    entry.artifact for entry in list(current.ready_feeds or [])
                ]
                parent_artifact = (
                    queued_artifacts[-1] if queued_artifacts else current.active_feed
                )
                next_rotation_epoch = current.rotation_epoch + len(queued_artifacts) + 1
                build_taste = (
                    self._rotation_taste(
                        taste,
                        parent_artifact,
                        prior_artifacts=[
                            artifact
                            for artifact in [current.active_feed, *queued_artifacts]
                            if artifact is not None and artifact is not parent_artifact
                        ],
                        reason=reason,
                        rotation_epoch=next_rotation_epoch,
                        inventory_generation=inventory.generation_id,
                    )
                    if current.active_feed is not None
                    else taste
                )
                artifact_started = time.perf_counter()
                artifact = self._build_artifact(
                    req,
                    taste=build_taste,
                    inventory=inventory,
                    parent_artifact=parent_artifact,
                    artifact_source="background_prepare",
                    request_id=str(uuid.uuid4()),
                )
                phase_timings["artifact_composition_ms"] = int(
                    (time.perf_counter() - artifact_started) * 1000
                )
                metadata_started = time.perf_counter()
                artifact, published_metadata_pending = (
                    hydrate_artifact_release_metadata(
                        self._server,
                        artifact,
                        allow_remote_lookup=not compose_from_ready_inventory and not targeted_radio_work,
                    )
                )
                phase_timings["metadata_ms"] = int((time.perf_counter() - metadata_started) * 1000)
                artifact.diagnostics["feed_prepare_ms"] = int(
                    (time.perf_counter() - prepare_started) * 1000
                )
                artifact.diagnostics["preparation_timing_ms"] = {
                    **phase_timings,
                    "total_ms": int((time.perf_counter() - prepare_started) * 1000),
                    "slot": len(current.ready_feeds or []) + 1,
                    "depth": len(current.ready_feeds or []),
                    "reason": reason,
                    "outcome": "built",
                }
                artifact.diagnostics["catalog_population_result"] = str(
                    catalog_result.get("reason") or ""
                )
                artifact.diagnostics["inventory_generation_id"] = (
                    inventory.generation_id
                )
                artifact.diagnostics["intent_delta_version"] = inventory.intent_version
                artifact.diagnostics["candidate_inventory_coverage"] = dict(
                    ready_inventory.coverage or {}
                )
                artifact.diagnostics["candidate_enrichment"] = dict(
                    materialized_supply.diagnostics
                    if materialized_supply is not None
                    else {
                        "reused_persisted_ready_inventory": True,
                    }
                )
                artifact.diagnostics["local_only_path"] = bool(local_only_path)
                artifact.diagnostics["provider_network_work_count"] = 0 if local_only_path else int(
                    (materialized_supply.diagnostics if materialized_supply is not None else {}).get(
                        "completed_request_count", 0
                    )
                    or 0
                )
                artifact.diagnostics["preparation_phase_markers"] = {
                    "state_reload": True,
                    "coverage_readiness": True,
                    "radio_selection": True,
                    "transient_injection": bool(local_only_path),
                    "artifact_composition": True,
                    "persistence": True,
                }
                if novelty_delta_requested:
                    artifact.diagnostics["queue_novelty_delta"] = {
                        "requested": True,
                        "bounded_cycles": 1,
                        "reused_request_progress": bool(
                            dict(inventory.acquisition_ledger or {}).get(
                                "request_progress"
                            )
                        ),
                    }
                metadata_deferred = bool(
                    compose_from_ready_inventory or targeted_radio_work
                )
                artifact.diagnostics["release_metadata_deferred"] = bool(
                    published_metadata_pending > 0 and metadata_deferred
                )
                if published_metadata_pending > 0 and not metadata_deferred:
                    if not lease_current():
                        return
                    current.generation_status = "inventory_building"
                    current.dirty_reasons = [
                        f"release_metadata_pending:{published_metadata_pending}"
                    ]
                    save_feed_state(
                        self._server,
                        current,
                        expected_preparation_lease_token=lease_token,
                    )
                    self._schedule_preparation(
                        req,
                        taste,
                        reason="release_metadata_replenish",
                    )
                    return
                if not artifact.accepted:
                    shortages = list(
                        (artifact.diagnostics or {}).get("row_shortage_domains") or []
                    )
                    if shortages:
                        shortage_inventory = inventory_with_row_shortages(
                            ready_inventory,
                            shortages,
                            quality_reasons=list(artifact.quality_reasons or []),
                        )
                        if not lease_current():
                            return
                        stored_shortage = store_candidate_inventory(
                            self._server,
                            shortage_inventory,
                            expected_ready_generation_id=ready_inventory.generation_id,
                        )
                        if stored_shortage:
                            ready_inventory = shortage_inventory
                        if not lease_current():
                            return
                        mark_feed_replenishing(self._server, current, shortages)
                        print(
                            f"[EBB:feed-state][replenish] scope={taste.user_scope_id} "
                            f"shortages={','.join(str(value) for value in shortages)} "
                            f"inventory_saved={1 if stored_shortage else 0} "
                            f"immediate_retry={1 if reason != 'inventory_replenish' else 0}"
                        )
                        if reason != "inventory_replenish":
                            self._schedule_preparation(
                                req,
                                taste,
                                reason="inventory_replenish",
                            )
                        return
                    if not lease_current():
                        return
                    mark_feed_build_failed(
                        self._server,
                        current,
                        "artifact_rejected_without_candidate_shortage:"
                        + ",".join(
                            str(value) for value in artifact.quality_reasons or []
                        ),
                    )
                    return
                if current.active_feed is None:
                    if not lease_current():
                        return
                    stored = store_active_feed(
                        self._server,
                        current,
                        artifact,
                        profile_fingerprint=taste.profile_key,
                        inventory_generation=inventory.generation_id,
                        rotation_epoch=current.rotation_epoch,
                        expected_active_version=base_active_version,
                        expected_preparation_lease_token=lease_token,
                    )
                    if stored is not None:
                        self._schedule_preparation(
                            req, taste, reason="post_initial_active"
                        )
                elif self._artifact_signature(
                    current.active_feed
                ) != self._artifact_signature(artifact):
                    if not lease_current():
                        return
                    store_started = time.perf_counter()
                    stored_prepared = store_prepared_feed(
                        self._server,
                        current,
                        artifact,
                        expected_active_version=base_active_version,
                        expected_inventory_generation=base_inventory_generation or None,
                        inventory_generation=inventory.generation_id,
                        rotation_epoch=next_rotation_epoch,
                        intent_version=inventory.intent_version,
                        expected_preparation_lease_token=lease_token,
                    )
                    store_ms = int((time.perf_counter() - store_started) * 1000)
                    timing = artifact.diagnostics.setdefault(
                        "preparation_timing_ms", {}
                    )
                    timing["persistence_store_ms"] = store_ms
                    timing["total_ms"] = int(
                        (time.perf_counter() - prepare_started) * 1000
                    )
                    timing["outcome"] = getattr(
                        stored_prepared, "outcome", "persistence_failure"
                    )
                    print(
                        "[EBB:feed-prepare] "
                        f"scope={taste.user_scope_id} slot={timing.get('slot')} "
                        f"depth={timing.get('depth')} reason={reason} "
                        f"outcome={timing.get('outcome')} "
                        f"timings={timing}",
                        flush=True,
                    )
                    if not stored_prepared or stored_prepared.outcome != "stored":
                        if getattr(stored_prepared, "reason", "") == "preparation_lease_superseded":
                            return
                        latest = (
                            load_feed_state(self._server, taste.user_scope_id)
                            or current
                        )
                        outcome = getattr(stored_prepared, "outcome", "persistence_failure")
                        if outcome == "duplicate" and reason not in {"queue_novelty_delta", "inventory_replenish"}:
                            # One bounded novelty delta per parent/inventory.
                            self._schedule_queue_recovery_once(
                                req, taste, latest,
                                outcome="duplicate",
                                reason="queue_novelty_delta",
                                inventory_generation=inventory.generation_id,
                            )
                        elif outcome == "contract_shortage":
                            # A shortage gets one targeted repair; do not fan out
                            # broad providers from a locally sufficient inventory.
                            if not lease_current():
                                return
                            mark_feed_dirty(
                                self._server,
                                latest,
                                "targeted_row_repair",
                            )
                            if reason != "targeted_row_repair":
                                self._schedule_queue_recovery_once(
                                    req, taste, latest,
                                    outcome="contract_shortage",
                                    reason="targeted_row_repair",
                                    inventory_generation=inventory.generation_id,
                                )
                        elif outcome in {"version_race", "inventory_race"}:
                            # State changed underneath us; a local recomposition
                            # will use the newly loaded parent/inventory.
                            self._schedule_preparation(req, taste, reason="queue_replenish")
                        elif outcome == "persistence_failure":
                            if not lease_current():
                                return
                            self._schedule_queue_recovery_once(
                                req, taste, latest,
                                outcome="persistence_failure",
                                reason="queue_store_retry",
                                inventory_generation=inventory.generation_id,
                            )
                        elif outcome == "quality_rejection":
                            if not lease_current():
                                return
                            mark_feed_dirty(self._server, latest, "quality_rejection")
                    elif stored_prepared.outcome == "stored" and len(stored_prepared.ready_feeds or []) < 2:
                        self._schedule_preparation(req, taste, reason="queue_replenish")
                    elif stored_prepared.outcome == "stored" and len(stored_prepared.ready_feeds or []) >= 2:
                        # Reservoir expansion is strictly post-queue work. The
                        # local critical path must first reach depth 2.
                        self._schedule_radio_reservoir_expansion(
                            req,
                            taste,
                            radio_inventory,
                        )
                    if published_metadata_pending > 0 and metadata_deferred:
                        self._schedule_preparation_after_response(
                            req,
                            reason="release_metadata_replenish",
                            dedupe_key=f"release-metadata:{taste.user_scope_id}",
                        )
                else:
                    pending_release_metadata = int(
                        dict(inventory.acquisition_ledger or {})
                        .get("release_metadata", {})
                        .get("pending_lookup_count")
                        or 0
                    )
                    current.generation_status = "ready"
                    current.dirty_reasons = (
                        [f"release_metadata_pending:{pending_release_metadata}"]
                        if pending_release_metadata > 0
                        else ["rotation_inventory_exhausted"]
                    )
                    save_feed_state(self._server, current)
                    if pending_release_metadata > 0:
                        self._schedule_preparation(
                            req,
                            taste,
                            reason="release_metadata_replenish",
                        )
            except Exception as exc:
                current = load_feed_state(self._server, taste.user_scope_id)
                if current is None:
                    current = FeedState(user_scope_id=taste.user_scope_id)
                mark_feed_build_failed(
                    self._server,
                    current,
                    f"prepare_exception:{type(exc).__name__}",
                )
                print(
                    f"[EBB:feed-state][error] scope={taste.user_scope_id} "
                    f"reason={reason} error={exc}"
                )
            finally:
                # A worker must never disappear while leaving a durable
                # ``preparing``/``inventory_building`` marker behind.  Those
                # states describe active work only; once this worker exits,
                # retain the active feed and expose a truthful delayed state
                # with an explicit retry hint.  A successor worker may update
                # this state immediately after the lease is released.
                retry_payload = None
                try:
                    terminal = load_feed_state(self._server, taste.user_scope_id)
                    if (
                        terminal is not None
                        and str(terminal.preparation_lease_token or "") == lease_token
                    ):
                        retry_at = time.time() + 5.0
                        retry_reason = (
                            str(terminal.preparation_lease_reason or "").strip()
                            or reason
                            or "queue_replenish"
                        )
                        if terminal.generation_status in {"preparing", "inventory_building"}:
                            terminal.generation_status = "delayed"
                        terminal.preparation_lease_started_at = 0.0
                        terminal.preparation_lease_deadline = 0.0
                        terminal.preparation_lease_reason = ""
                        terminal.preparation_lease_token = ""
                        terminal.retry_at = retry_at
                        terminal.retry_reason = retry_reason
                        terminal.dirty_reasons = [
                            *[
                                str(value)
                                for value in (terminal.dirty_reasons or [])
                                if not str(value).startswith("retry_at:")
                            ],
                            f"retry_at:{retry_at:.3f}",
                        ]
                        save_feed_state(
                            self._server,
                            terminal,
                            expected_preparation_lease_token=lease_token,
                        )
                        retry_payload = (req, taste, retry_reason)
                except Exception:
                    # Cleanup must not mask the original worker outcome.
                    pass
                self._release_background_build(fingerprint)
                pending = None
                with self._background_build_lock:
                    pending = self._pending_background_builds.pop(fingerprint, None)
                if pending is not None:
                    pending_req, pending_taste, pending_reason = pending
                    self._schedule_preparation(
                        pending_req,
                        pending_taste,
                        reason=pending_reason,
                    )
                elif retry_payload is not None:
                    retry_req, retry_taste, retry_reason = retry_payload
                    timer = threading.Timer(
                        5.0,
                        lambda: self._schedule_preparation(
                            retry_req,
                            retry_taste,
                            reason=retry_reason,
                        ),
                    )
                    timer.daemon = True
                    timer.start()

        self._prepare_executor.submit(prepare)
        return

    def _row_page(self, req: Any, *, request_id: str) -> Dict[str, Any]:
        row_id = self._trim(getattr(req, "row_id", ""))
        session_id = self._trim(getattr(req, "session_id", ""))
        offset = max(int(getattr(req, "offset", 0) or 0), 0)
        limit = max(int(getattr(req, "limit", 8) or 8), 1)
        session = load_feed_session(self._server, session_id)
        if isinstance(session, dict) and row_id:
            artifact = self._artifact_from_session(session)
            if artifact is not None:
                response = row_page_response_from_artifact(
                    artifact,
                    row_id=row_id,
                    offset=offset,
                    limit=limit,
                    request_id=request_id,
                )
                if response is not None:
                    return response
        feed_state = load_feed_state(
            self._server,
            self._trim(getattr(req, "user_scope_id", "")) or "guest",
        )
        cached = feed_state.active_feed if feed_state is not None else None
        if cached is not None and row_id:
            response = row_page_response_from_artifact(
                cached,
                row_id=row_id,
                offset=offset,
                limit=limit,
                request_id=request_id,
            )
            if response is not None:
                return response
        raise HTTPException(status_code=404, detail="Discovery row is not available")

    def _build_artifact(
        self,
        req: Any,
        *,
        taste: Any | None = None,
        inventory: CandidateInventory | None = None,
        parent_artifact: DiscoveryArtifact | None = None,
        artifact_source: str,
        request_id: str,
    ) -> DiscoveryArtifact:
        started = time.perf_counter()
        taste = taste or build_taste_profile(self._server, req)
        now = time.time()
        if taste.is_cold_start:
            diagnostics = build_diagnostics(
                artifact_source=artifact_source,
                artifact_quality="rejected",
                row_status={},
                rows=[],
                candidate_pool_counts={},
                provider_timings_ms={},
                home_tab_lanes={},
                home_tab_diagnostics={
                    "accepted": False,
                    "rejection_reasons": ["cold_start"],
                },
                quality_reasons=["cold_start_not_cached_as_personalized"],
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                taste=taste,
            )
            diagnostics["fresh_account_empty_home"] = True
            diagnostics["client_signal_tier"] = "cold_start"
            return DiscoveryArtifact(
                session_id=str(uuid.uuid4()),
                user_scope_id=taste.user_scope_id,
                profile_key=taste.profile_key,
                generated_at=now,
                expires_at=now + ARTIFACT_TTL_SECONDS,
                rows=[],
                diagnostics=diagnostics,
                candidate_pool_counts={},
                provider_timings_ms={},
                home_tab_lanes={},
                accepted=False,
                quality_reasons=["cold_start_not_cached_as_personalized"],
                artifact_source=artifact_source,
            )

        inventory = inventory or load_candidate_inventory(
            self._server,
            taste.user_scope_id,
            profile_fingerprint=taste.profile_key,
            require_fresh=False,
        )
        if inventory is None:
            diagnostics = build_diagnostics(
                artifact_source=artifact_source,
                artifact_quality="build_failed",
                row_status={},
                rows=[],
                candidate_pool_counts={},
                provider_timings_ms={},
                home_tab_lanes={},
                home_tab_diagnostics={"accepted": False},
                quality_reasons=["candidate_inventory_unavailable"],
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                taste=taste,
            )
            return DiscoveryArtifact(
                session_id=str(uuid.uuid4()),
                user_scope_id=taste.user_scope_id,
                profile_key=taste.profile_key,
                generated_at=now,
                expires_at=now + ARTIFACT_TTL_SECONDS,
                rows=[],
                diagnostics=diagnostics,
                candidate_pool_counts={},
                provider_timings_ms={},
                home_tab_lanes={},
                accepted=False,
                quality_reasons=["candidate_inventory_unavailable"],
                artifact_source=artifact_source,
            )
        pools = {
            name: list(candidates or []) for name, candidates in inventory.pools.items()
        }
        candidate_counts = dict(inventory.candidate_counts or {})
        provider_timings = dict(inventory.provider_timings_ms or {})
        rows, row_status, home_tab_lanes, home_tab_diagnostics = build_rows_from_pools(
            pools,
            taste,
        )
        active_state = load_feed_state(self._server, taste.user_scope_id)
        rows, preserved_optional_rows = _preserve_complete_optional_rows(
            rows,
            parent_artifact
            if parent_artifact is not None
            else (active_state.active_feed if active_state is not None else None),
        )
        for kind in preserved_optional_rows:
            preserved_row = next(row for row in rows if row.kind == kind)
            row_status[kind] = {
                "status": "emitted",
                "count": len(preserved_row.items or []),
                "warnings": ["preserved_active_until_replacement"],
            }
        accepted, quality_reasons, artifact_quality = evaluate_quality(
            rows=rows,
            taste=taste,
            home_tab_diagnostics=home_tab_diagnostics,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        diagnostics = build_diagnostics(
            artifact_source=artifact_source,
            artifact_quality=artifact_quality,
            row_status=row_status,
            rows=rows,
            candidate_pool_counts=candidate_counts,
            provider_timings_ms=provider_timings,
            home_tab_lanes=home_tab_lanes,
            home_tab_diagnostics=home_tab_diagnostics,
            quality_reasons=quality_reasons,
            elapsed_ms=elapsed_ms,
            taste=taste,
        )
        diagnostics["request_id"] = request_id
        diagnostics["model_version"] = ENGINE_MODEL_VERSION
        diagnostics["feed_promotion_contract"] = FEED_PROMOTION_CONTRACT_VERSION
        diagnostics["client_signal_tier"] = taste.signal_tier
        diagnostics["profile_key"] = taste.profile_key
        diagnostics["refresh_requested"] = bool(taste.force_refresh)
        diagnostics["avoid_ids_count"] = len(taste.avoid_ids or [])
        diagnostics["refresh_token_present"] = bool(
            str(taste.refresh_token or "").strip()
        )
        diagnostics["candidate_inventory_model"] = inventory.model_version
        diagnostics["inventory_generation_id"] = inventory.generation_id
        diagnostics["intent_delta_version"] = inventory.intent_version
        diagnostics["candidate_inventory_age_ms"] = max(
            int((time.time() - inventory.generated_at) * 1000),
            0,
        )
        diagnostics["feed_external_calls"] = 0
        diagnostics["inventory_external_call_groups"] = int(
            candidate_counts.get("inventory_external_call_groups") or 0
        )
        diagnostics["feed_rotation_reason"] = artifact_source
        diagnostics["preserved_optional_rows"] = preserved_optional_rows
        return DiscoveryArtifact(
            session_id=str(uuid.uuid4()),
            user_scope_id=taste.user_scope_id,
            profile_key=taste.profile_key,
            generated_at=now,
            expires_at=now + ARTIFACT_TTL_SECONDS,
            rows=rows,
            diagnostics=diagnostics,
            candidate_pool_counts=candidate_counts,
            provider_timings_ms=provider_timings,
            home_tab_lanes=home_tab_lanes,
            accepted=accepted,
            quality_reasons=quality_reasons,
            artifact_source=artifact_source,
        )

    def _store_session(self, artifact: DiscoveryArtifact) -> None:
        session_id = str(artifact.session_id or "").strip()
        if not session_id:
            return
        with self._stored_session_lock:
            store_session = session_id not in self._stored_session_ids
            if store_session:
                self._stored_session_ids.add(session_id)
                if len(self._stored_session_ids) > 512:
                    self._stored_session_ids = {session_id}

        def persist_side_effects() -> None:
            try:
                session = artifact_to_session(artifact)
                if store_session:
                    _store_feed_session(self._server, session)
                visible_rows = []
                page_size = max(
                    int(getattr(self._server, "RECOMMENDATION_ROW_PAGE_SIZE", 8) or 8),
                    1,
                )
                for row in session.get("rows") or []:
                    if not isinstance(row, dict):
                        continue
                    visible = dict(row)
                    visible["items"] = list(row.get("items") or [])[:page_size]
                    visible_rows.append(visible)
                self._server._recommendation_record_impressions(session, visible_rows)
            except Exception:
                return

        executor = getattr(self._server, "precompute_executor", None)
        if executor is not None:
            try:
                executor.submit(persist_side_effects)
                return
            except Exception:
                pass
        try:
            persist_side_effects()
        except Exception:
            return

    def _artifact_from_session(
        self, session: Dict[str, Any]
    ) -> DiscoveryArtifact | None:
        rows = []
        from .artifact import _artifact_from_dict

        payload = {
            "artifact_version": ARTIFACT_VERSION,
            "session_id": session.get("session_id"),
            "user_scope_id": session.get("user_scope_id") or "guest",
            "profile_key": session.get("profile_key") or "",
            "generated_at": session.get("generated_at") or time.time(),
            "expires_at": session.get("expires_at")
            or time.time() + ARTIFACT_TTL_SECONDS,
            "rows": session.get("rows") or rows,
            "diagnostics": session.get("diagnostics") or {},
            "candidate_pool_counts": (session.get("diagnostics") or {}).get(
                "candidate_pool_counts"
            )
            or {},
            "provider_timings_ms": (session.get("diagnostics") or {}).get(
                "provider_timings_ms"
            )
            or {},
            "home_tab_lanes": (session.get("diagnostics") or {}).get("home_tab_lanes")
            or {},
            "accepted": True,
            "quality_reasons": (session.get("diagnostics") or {}).get("quality_reasons")
            or [],
            "artifact_source": "cache",
        }
        return _artifact_from_dict(payload)

    def _trim(self, value: Any) -> str:
        try:
            return self._server._recommendation_trim_text(value)
        except Exception:
            return str(value or "").strip()
