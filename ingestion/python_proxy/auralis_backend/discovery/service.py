from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Dict
import hashlib
import json
import threading
import time
import uuid

from fastapi import HTTPException

from ..recommend.session_runtime import _store_feed_session, load_feed_session
from ..search.catalog_pipeline import schedule_catalog_population
from .adapters import artifact_to_session, home_response_from_artifact, row_page_response_from_artifact
from .artifact import build_diagnostics, evaluate_quality
from .config import (
    ARTIFACT_TTL_SECONDS,
    ARTIFACT_VERSION,
    ENGINE_MODEL_VERSION,
    ROW_ORDER,
    ROW_RECIPES,
)
from .feed_state import (
    FeedState,
    load_feed_state,
    mark_feed_build_failed,
    mark_feed_dirty,
    mark_feed_replenishing,
    promote_prepared_feed,
    save_feed_state,
    store_active_feed,
    store_prepared_feed,
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
    clear_inventory_intent_delta,
    load_candidate_inventory,
    load_inventory_intent_delta,
    inventory_with_row_shortages,
    refresh_candidate_inventory_coverage,
    store_candidate_inventory,
)
from .radio_inventory import (
    build_artist_radio_inventory,
    load_artist_radio_inventory,
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
        self._last_background_builds: Dict[str, float] = {}
        self._background_builds_inflight: set[str] = set()
        self._background_build_tokens: Dict[str, str] = {}
        self._pending_background_builds: Dict[str, tuple[Any, Any, str]] = {}
        self._signal_reconciliations_inflight: set[str] = set()
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
        def item_signature(item: Dict[str, Any]) -> Any:
            identity = str(
                item.get("canonical_entity_id")
                or item.get("canonical_track_identity")
                or item.get("canonical_source_identity")
                or item.get("id")
                or item.get("videoId")
                or item.get("title")
                or ""
            )
            nested = item.get("tracks") or item.get("items")
            return [
                identity,
                str(
                    item.get("release_year")
                    or item.get("year")
                    or item.get("release_date")
                    or ""
                ),
                [item_signature(track) for track in nested if isinstance(track, dict)]
                if isinstance(nested, list)
                else [],
            ]
        payload = [
            [
                row.kind,
                [item_signature(item) for item in row.items or []],
            ]
            for row in artifact.rows or []
        ]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()

    def _schedule_preparation_after_response(
        self,
        req: Any,
        *,
        reason: str,
        dedupe_key: str = "",
    ) -> None:
        """Reconcile current signals without extending a feed response."""

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

    def _changed_row_kinds(
        self,
        previous: DiscoveryArtifact | None,
        current: DiscoveryArtifact,
    ) -> list[str]:
        def row_signatures(artifact: DiscoveryArtifact | None) -> Dict[str, tuple[str, ...]]:
            if artifact is None:
                return {}
            output: Dict[str, tuple[str, ...]] = {}
            for row in artifact.rows or []:
                values: list[str] = []
                for item in row.items or []:
                    identity = str(
                        item.get("canonical_entity_id")
                        or item.get("canonical_track_identity")
                        or item.get("canonical_source_identity")
                        or item.get("id")
                        or item.get("videoId")
                        or item.get("title")
                        or ""
                    ).strip()
                    if identity:
                        values.append(identity)
                    nested = item.get("tracks") or item.get("items")
                    if isinstance(nested, list):
                        values.extend(
                            str(
                                track.get("canonical_entity_id")
                                or track.get("canonical_track_identity")
                                or track.get("canonical_source_identity")
                                or track.get("id")
                                or track.get("videoId")
                                or ""
                            ).strip()
                            for track in nested
                            if isinstance(track, dict)
                        )
                output[row.kind] = tuple(value for value in values if value)
            return output

        previous_rows = row_signatures(previous)
        current_rows = row_signatures(current)
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
            if (
                fingerprint in self._background_builds_inflight
                or (not urgent and time.time() - last_build < 45.0)
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
        reason: str,
        rotation_epoch: int = 0,
        inventory_generation: str = "",
    ) -> Any:
        avoid_ids = list(
            dict.fromkeys(
                [
                    *list(getattr(taste, "avoid_ids", []) or []),
                    *self._visible_track_ids(active),
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
        scope = self._trim(getattr(req, "user_scope_id", "")) or "guest"

        # Search return must never wait for a search-shaped feed build. Promote
        # the successor that is already ready, then use the new search intent
        # while preparing the following successor in the background.
        if request_mode == "full_feed" and session_requested:
            session_state = load_feed_state(self._server, scope)
            if session_state is not None:
                promoted = (
                    promote_prepared_feed(self._server, session_state)
                    if session_state.prepared_feed is not None
                    else None
                )
                visible = promoted or session_state.active_feed
                if visible is not None:
                    if promoted is not None:
                        intent_version = int(
                            (promoted.diagnostics or {}).get(
                                "intent_delta_version"
                            )
                            or 0
                        )
                        if intent_version > 0:
                            clear_inventory_intent_delta(
                                self._server,
                                scope,
                                consumed_version=intent_version,
                            )
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
                        action=(
                            "promoted_prepared"
                            if promoted is not None
                            else "served_active"
                        ),
                        changed=promoted is not None,
                        reason=(
                            ""
                            if promoted is not None
                            else "search_intent_preparing_successor"
                        ),
                    )

        # Pull-to-refresh normally promotes a successor that has already been
        # validated and persisted. That promotion does not need a fresh taste
        # rebuild in the request path. Reconciliation prepares the following
        # successor after this response has been returned.
        if request_mode == "full_feed" and force_requested and not session_requested:
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
                refresh_token = str(
                    getattr(req, "refresh_token", "") or ""
                ).strip()
                reconcile_key = f"refresh:{scope}:{refresh_token}"
                if not self._refresh_work_is_running(scope, req):
                    self._schedule_preparation_after_response(
                        req,
                        reason="pull_to_refresh",
                        dedupe_key=reconcile_key,
                    )
                return self._feed_response(
                    refresh_state.active_feed,
                    state=refresh_state,
                    request_id=request_id,
                    page_size=page_size,
                    action="unchanged_no_rotation",
                    changed=False,
                    reason="refresh_preparing_successor",
                )

        # Serving an existing feed does not require rebuilding the complete
        # taste profile. History reconciliation and successor preparation can
        # run after the response instead of delaying every cold launch.
        if request_mode == "full_feed" and not force_requested and not session_requested:
            launch_state = load_feed_state(self._server, scope)
            if launch_state is not None and launch_state.active_feed is not None:
                needs_successor = bool(
                    (launch_state.prepared_feed is None or launch_state.dirty_reasons)
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
            mark_feed_dirty(self._server, state, "profile_changed")
        if force_refresh:
            mark_feed_dirty(self._server, state, "pull_to_refresh")
        if session_intent:
            mark_feed_dirty(self._server, state, "search_session_intent")

        if force_refresh or session_intent:
            promoted = (
                promote_prepared_feed(self._server, state)
                if force_refresh and not session_intent
                else None
            )
            if promoted is not None:
                intent_version = int((promoted.diagnostics or {}).get("intent_delta_version") or 0)
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
                        current = load_feed_state(self._server, taste.user_scope_id) or state
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
            inventory = load_candidate_inventory(
                self._server,
                taste.user_scope_id,
                profile_fingerprint=taste.profile_key,
                require_fresh=False,
            )
            if inventory is not None:
                candidate = self._build_artifact(
                    req,
                    taste=taste,
                    inventory=inventory,
                    artifact_source="candidate_inventory",
                    request_id=request_id,
                )
                if candidate.accepted and self._artifact_signature(candidate) != self._artifact_signature(state.active_feed):
                    if state.active_feed is None:
                        stored = store_active_feed(
                            self._server,
                            state,
                            candidate,
                            profile_fingerprint=taste.profile_key,
                            inventory_generation=inventory.generation_id,
                            rotation_epoch=state.rotation_epoch,
                            expected_active_version=state.active_version,
                        )
                        if stored is not None:
                            self._schedule_preparation(req, taste, reason="post_initial_active")
                            return self._feed_response(
                                candidate,
                                state=state,
                                request_id=request_id,
                                page_size=page_size,
                                action="built_and_promoted",
                                changed=True,
                            )
                    store_prepared_feed(
                        self._server,
                        state,
                        candidate,
                        expected_active_version=state.active_version,
                        expected_inventory_generation=state.active_inventory_generation or None,
                        inventory_generation=inventory.generation_id,
                        rotation_epoch=state.rotation_epoch + 1,
                        intent_version=inventory.intent_version,
                    )
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
                intent_version = int((promoted.diagnostics or {}).get("intent_delta_version") or 0)
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
            if (
                inventory is None
                or (not prepared_matches_profile and not rotation_exhausted)
            ):
                self._schedule_preparation(
                    req,
                    taste,
                    reason=(
                        "launch_stale_inventory"
                        if inventory is None or state.profile_fingerprint != taste.profile_key
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
            reason="initial_retry" if state.generation_status == "build_failed" else "initial_feed",
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
        artifact.diagnostics = dict(artifact.diagnostics or {})
        artifact.diagnostics.update(
            {
                "feed_action": action,
                "feed_state_version": "feed-state",
                "feed_version": state.active_version,
                "preparation_state": state.generation_status,
                "prepared_candidate_available": state.prepared_feed is not None,
                "rotation_inventory_exhausted": state.dirty_reasons == ["rotation_inventory_exhausted"],
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
            quality_reasons=["initial_feed_build_failed" if build_failed else "initial_feed_not_ready"],
            artifact_source=action,
        )
        return home_response_from_artifact(
            artifact,
            request_id=request_id,
            page_size=page_size,
        )

    def _schedule_preparation(self, req: Any, taste: Any, *, reason: str) -> None:
        fingerprint = self._background_fingerprint(taste)
        refresh_token = str(getattr(req, "refresh_token", "") or "").strip()
        urgent = reason in {
            "pull_to_refresh",
            "search_session_intent",
            "post_refresh",
            "post_promotion",
            "post_initial_active",
            "inventory_replenish",
            "release_metadata_replenish",
        }
        if not self._claim_background_build(
            fingerprint,
            urgent=urgent,
            refresh_token=refresh_token,
        ):
            if urgent:
                with self._background_build_lock:
                    if fingerprint in self._background_builds_inflight:
                        inflight_token = self._background_build_tokens.get(fingerprint, "")
                        # Polling the same explicit refresh observes the
                        # running build; it is not a request for another one.
                        if not refresh_token or refresh_token != inflight_token:
                            self._pending_background_builds[fingerprint] = (req, taste, reason)
            return
        building_state = load_feed_state(self._server, taste.user_scope_id)
        if building_state is None:
            building_state = FeedState(user_scope_id=taste.user_scope_id)
        if building_state.active_feed is None:
            building_state.generation_status = "inventory_building"
            save_feed_state(self._server, building_state)

        def prepare() -> None:
            prepare_started = time.perf_counter()
            try:
                previous_inventory = load_candidate_inventory(
                    self._server,
                    taste.user_scope_id,
                    profile_fingerprint=taste.profile_key,
                    require_fresh=False,
                )
                if previous_inventory is not None:
                    refreshed_previous = refresh_candidate_inventory_coverage(
                        previous_inventory,
                        taste=taste,
                    )
                    if refreshed_previous.acquisition_ledger != previous_inventory.acquisition_ledger:
                        store_candidate_inventory(
                            self._server,
                            refreshed_previous,
                            expected_ready_generation_id=previous_inventory.generation_id,
                        )
                    previous_inventory = refreshed_previous
                catalog_result = schedule_catalog_population(
                    self._server,
                    user_scope_id=taste.user_scope_id,
                    req=req,
                    taste=taste,
                    reason=f"feed_inventory_{reason}",
                    min_interval_seconds=300.0,
                    wait_for_completion=False,
                    wait_timeout_seconds=0.0,
                )
                ready_inventory = previous_inventory
                working_inventory = previous_inventory
                active_optional_rows = (
                    _artifact_row_kinds(building_state.active_feed)
                    & _OPTIONAL_ROW_KINDS
                )
                persisted_optional_rows = _completed_optional_rows(
                    previous_inventory.coverage if previous_inventory is not None else {}
                )
                # A completed optional shelf may already be waiting in the
                # persisted inventory from an interrupted worker. Build its
                # prepared feed immediately instead of making it fetch again.
                prepared_new_inventory = bool(
                    previous_inventory is not None
                    and previous_inventory.is_ready
                    and (persisted_optional_rows - active_optional_rows)
                )
                if (
                    reason == "release_metadata_replenish"
                    and previous_inventory is not None
                    and previous_inventory.is_ready
                ):
                    metadata_inventory = complete_inventory_release_metadata(
                        self._server,
                        previous_inventory,
                    )
                    if store_candidate_inventory(
                        self._server,
                        metadata_inventory,
                        expected_ready_generation_id=previous_inventory.generation_id,
                    ):
                        ready_inventory = metadata_inventory
                        working_inventory = metadata_inventory
                        prepared_new_inventory = True
                materialized_supply = None
                max_cycles = (
                    6
                    if building_state.active_feed is not None
                    else max(
                        2,
                        min(6, ((len(taste.full_history_tracks) + 23) // 24) + 1),
                    )
                )
                for _cycle in range(0 if prepared_new_inventory else max_cycles):
                    enrichment_plan = build_enrichment_plan(
                        taste,
                        acquisition_ledger=(
                            working_inventory.acquisition_ledger
                            if working_inventory is not None
                            else {}
                        ),
                    )
                    materialized_supply = materialize_enrichment_plan(
                        self._server,
                        enrichment_plan,
                        time_budget_seconds=None,
                        max_workers=6,
                        max_pending_jobs=6,
                    )
                    materialized_supply = verify_materialized_supply(
                        self._server,
                        materialized_supply,
                        taste,
                        max_new_verifications=None,
                        max_workers=4,
                    )
                    candidate_inventory = build_candidate_inventory(
                        self._server,
                        taste,
                        previous=working_inventory,
                        materialized_supply=materialized_supply,
                    )
                    candidate_inventory = complete_inventory_release_metadata(
                        self._server,
                        candidate_inventory,
                    )
                    radio_taste = self._rotation_taste(
                        taste,
                        building_state.active_feed,
                        reason=reason,
                        rotation_epoch=building_state.rotation_epoch
                        + (1 if building_state.active_feed is not None else 0),
                        inventory_generation=candidate_inventory.generation_id,
                    )
                    built_radio_inventory = build_artist_radio_inventory(
                        radio_taste,
                        candidate_inventory.pools,
                        server=self._server,
                    )
                    persisted_radio_inventory = load_artist_radio_inventory(
                        self._server,
                        taste.user_scope_id,
                    )
                    if built_radio_inventory.is_ready:
                        radio_inventory = built_radio_inventory
                        store_artist_radio_inventory(self._server, radio_inventory)
                    elif (
                        persisted_radio_inventory is not None
                        and persisted_radio_inventory.is_ready
                    ):
                        radio_inventory = replace(
                            persisted_radio_inventory,
                            diagnostics={
                                **dict(persisted_radio_inventory.diagnostics or {}),
                                "reused_complete_inventory": True,
                                "replacement_card_count": len(
                                    built_radio_inventory.cards
                                ),
                            },
                        )
                    else:
                        radio_inventory = built_radio_inventory
                        store_artist_radio_inventory(self._server, radio_inventory)
                    candidate_pools = {
                        name: list(values or [])
                        for name, values in candidate_inventory.pools.items()
                    }
                    candidate_pools["popular_radio_cards"] = radio_card_candidates(
                        radio_inventory
                    )
                    candidate_counts = dict(candidate_inventory.candidate_counts or {})
                    candidate_counts["popular_radio_cards"] = len(radio_inventory.cards)
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
                    candidate_inventory = replace(
                        candidate_inventory,
                        pools=candidate_pools,
                        candidate_counts=candidate_counts,
                        coverage=coverage,
                        row_coverage={
                            "ready": coverage.get("ready") is True,
                            "actual": dict(coverage.get("actual") or {}),
                            "minimums": dict(coverage.get("minimums") or {}),
                            "failed_contracts": list(coverage.get("failed_contracts") or []),
                        },
                    )
                    working_inventory = candidate_inventory
                    if not candidate_inventory.is_ready:
                        continue
                    expected_generation = (
                        ready_inventory.generation_id if ready_inventory is not None else ""
                    )
                    if not store_candidate_inventory(
                        self._server,
                        candidate_inventory,
                        expected_ready_generation_id=expected_generation,
                    ):
                        current = load_feed_state(self._server, taste.user_scope_id)
                        if current is None:
                            current = FeedState(user_scope_id=taste.user_scope_id)
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
                    current.dirty_reasons = [
                        "inventory_shortage:" + ",".join(str(value) for value in shortages)
                    ] if shortages else ["inventory_acquisition_pending"]
                    save_feed_state(self._server, current)
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
                build_taste = self._rotation_taste(
                    taste,
                    current.active_feed,
                    reason=reason,
                    rotation_epoch=current.rotation_epoch + (1 if current.active_feed is not None else 0),
                    inventory_generation=inventory.generation_id,
                ) if current.active_feed is not None else taste
                artifact = self._build_artifact(
                    req,
                    taste=build_taste,
                    inventory=inventory,
                    artifact_source="background_prepare",
                    request_id=str(uuid.uuid4()),
                )
                artifact, published_metadata_pending = hydrate_artifact_release_metadata(
                    self._server,
                    artifact,
                )
                artifact.diagnostics["feed_prepare_ms"] = int(
                    (time.perf_counter() - prepare_started) * 1000
                )
                artifact.diagnostics["catalog_population_result"] = str(
                    catalog_result.get("reason") or ""
                )
                artifact.diagnostics["inventory_generation_id"] = inventory.generation_id
                artifact.diagnostics["intent_delta_version"] = inventory.intent_version
                artifact.diagnostics["candidate_inventory_coverage"] = dict(
                    ready_inventory.coverage or {}
                )
                artifact.diagnostics["candidate_enrichment"] = dict(
                    materialized_supply.diagnostics if materialized_supply is not None else {
                        "reused_persisted_ready_inventory": True,
                    }
                )
                if published_metadata_pending > 0:
                    current.generation_status = "inventory_building"
                    current.dirty_reasons = [
                        f"release_metadata_pending:{published_metadata_pending}"
                    ]
                    save_feed_state(self._server, current)
                    self._schedule_preparation(
                        req,
                        taste,
                        reason="release_metadata_replenish",
                    )
                    return
                if not artifact.accepted:
                    shortages = list(
                        (artifact.diagnostics or {}).get("row_shortage_domains")
                        or []
                    )
                    if shortages:
                        shortage_inventory = inventory_with_row_shortages(
                            ready_inventory,
                            shortages,
                            quality_reasons=list(artifact.quality_reasons or []),
                        )
                        stored_shortage = store_candidate_inventory(
                            self._server,
                            shortage_inventory,
                            expected_ready_generation_id=ready_inventory.generation_id,
                        )
                        if stored_shortage:
                            ready_inventory = shortage_inventory
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
                    stored = store_active_feed(
                        self._server,
                        current,
                        artifact,
                        profile_fingerprint=taste.profile_key,
                        inventory_generation=inventory.generation_id,
                        rotation_epoch=current.rotation_epoch,
                        expected_active_version=base_active_version,
                    )
                    if stored is not None:
                        self._schedule_preparation(req, taste, reason="post_initial_active")
                elif self._artifact_signature(current.active_feed) != self._artifact_signature(artifact):
                    store_prepared_feed(
                        self._server,
                        current,
                        artifact,
                        expected_active_version=base_active_version,
                        expected_inventory_generation=base_inventory_generation or None,
                        inventory_generation=inventory.generation_id,
                        rotation_epoch=current.rotation_epoch + 1,
                        intent_version=inventory.intent_version,
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
            home_tab_diagnostics={"accepted": False, "rejection_reasons": ["cold_start"]},
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
        pools = {name: list(candidates or []) for name, candidates in inventory.pools.items()}
        candidate_counts = dict(inventory.candidate_counts or {})
        provider_timings = dict(inventory.provider_timings_ms or {})
        rows, row_status, home_tab_lanes, home_tab_diagnostics = build_rows_from_pools(
            pools,
            taste,
        )
        active_state = load_feed_state(self._server, taste.user_scope_id)
        rows, preserved_optional_rows = _preserve_complete_optional_rows(
            rows,
            active_state.active_feed if active_state is not None else None,
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
        diagnostics["client_signal_tier"] = taste.signal_tier
        diagnostics["profile_key"] = taste.profile_key
        diagnostics["refresh_requested"] = bool(taste.force_refresh)
        diagnostics["avoid_ids_count"] = len(taste.avoid_ids or [])
        diagnostics["refresh_token_present"] = bool(str(taste.refresh_token or "").strip())
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

    def _artifact_from_session(self, session: Dict[str, Any]) -> DiscoveryArtifact | None:
        rows = []
        from .artifact import _artifact_from_dict

        payload = {
            "artifact_version": ARTIFACT_VERSION,
            "session_id": session.get("session_id"),
            "user_scope_id": session.get("user_scope_id") or "guest",
            "profile_key": session.get("profile_key") or "",
            "generated_at": session.get("generated_at") or time.time(),
            "expires_at": session.get("expires_at") or time.time() + ARTIFACT_TTL_SECONDS,
            "rows": session.get("rows") or rows,
            "diagnostics": session.get("diagnostics") or {},
            "candidate_pool_counts": (session.get("diagnostics") or {}).get("candidate_pool_counts") or {},
            "provider_timings_ms": (session.get("diagnostics") or {}).get("provider_timings_ms") or {},
            "home_tab_lanes": (session.get("diagnostics") or {}).get("home_tab_lanes") or {},
            "accepted": True,
            "quality_reasons": (session.get("diagnostics") or {}).get("quality_reasons") or [],
            "artifact_source": "cache",
        }
        return _artifact_from_dict(payload)

    def _trim(self, value: Any) -> str:
        try:
            return self._server._recommendation_trim_text(value)
        except Exception:
            return str(value or "").strip()
