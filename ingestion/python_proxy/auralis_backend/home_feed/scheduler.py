from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import hashlib
import json
import time

from .contracts import HomeJobRecord, HomeJobType
from .repositories import HomeRepositorySet, ImmutableRecordConflict


def request_payload(request: Any) -> dict[str, Any]:
    """Return the JSON-safe request needed to rebuild taste in a worker."""

    if request is None:
        return {}
    if isinstance(request, Mapping):
        value = dict(request)
    elif hasattr(request, "model_dump"):
        value = dict(request.model_dump(mode="json"))
    elif hasattr(request, "dict"):
        value = dict(request.dict())
    else:
        value = {
            key: raw
            for key, raw in vars(request).items()
            if not key.startswith("_")
        }
    # Round-trip once so unsupported process-local objects cannot enter the
    # durable queue and fail only after a restart.
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def _supply_priority(request: Mapping[str, Any], reason: str, default: int) -> int:
    if reason == "idle_reserve_enrichment":
        return 30
    if reason == "foreground_refresh" or request.get("force_refresh") or request.get("promote_ready_on_launch"):
        return 110
    return default


class HomeJobScheduler:
    """The only entry point for scheduling Home background work."""

    def __init__(self, server: Any) -> None:
        self._repositories = HomeRepositorySet.from_server(server)

    @property
    def repositories(self) -> HomeRepositorySet:
        return self._repositories

    def _enqueue(
        self,
        job_type: HomeJobType,
        request: Any,
        *,
        reason: str,
        profile_fingerprint: str = "",
        generation_hint: str = "",
        extra: Mapping[str, Any] | None = None,
        priority: int = 0,
        max_attempts: int = 5,
        run_after: float | None = None,
    ) -> HomeJobRecord:
        req = request_payload(request)
        scope = str(req.get("user_scope_id") or "guest").strip() or "guest"
        payload = {
            "request": req,
            "reason": str(reason or "background"),
            "profile_fingerprint": str(profile_fingerprint or ""),
            **dict(extra or {}),
        }
        # Generation keys identify one exact unit of work. Completed jobs are
        # immutable history; a later trigger receives a new key instead of
        # reviving a terminal row.
        hint = str(generation_hint or "").strip()
        if not hint:
            hint = f"{int(time.time() // 30)}:{_digest(payload)}"
        generation_key = f"{job_type.value}:{hint}"
        try:
            return self._repositories.jobs.enqueue(
                scope,
                job_type,
                generation_key,
                payload=payload,
                priority=priority,
                max_attempts=max_attempts,
                run_after=run_after,
            )
        except ImmutableRecordConflict:
            # A caller reused an explicit hint for different input. Preserve
            # immutable history and schedule the new input under its digest.
            return self._repositories.jobs.enqueue(
                scope,
                job_type,
                f"{generation_key}:{_digest(payload)}",
                payload=payload,
                priority=priority,
                max_attempts=max_attempts,
                run_after=run_after,
            )

    def reconcile_supply(self, request: Any, *, state: Any, reason: str) -> HomeJobRecord:
        req = request_payload(request)
        req["user_scope_id"] = state.user_scope_id
        # Coalesce across HTTP workers/restarts; ordinary visits must not hide
        # a later signal update or explicit refresh.
        intent = "signals" if reason == "background_prepare" else "refresh" if (
            reason in {"pull_to_refresh", "launch_missing_successor"}
            or req.get("force_refresh")
        ) else "maintenance"
        enqueue = (self._repositories.jobs.enqueue_singleton if intent == "refresh"
                   else self._repositories.jobs.enqueue_coalesced)
        return enqueue(
            state.user_scope_id, HomeJobType.RECONCILE_SUPPLY,
            f"check:{state.revision}:{int(time.time() // 30)}:{intent}",
            payload={"request": req, "reason": reason},
            priority=105, max_attempts=4,
            **({"expedite": True} if intent == "refresh" else {}),
        )

    def candidates(
        self,
        request: Any,
        *,
        reason: str,
        profile_fingerprint: str = "",
        generation_hint: str = "",
        coalesce_terminal: bool = False,
        run_after: float | None = None,
    ) -> HomeJobRecord:
        req = request_payload(request)
        scope = str(req.get("user_scope_id") or "guest").strip() or "guest"
        payload = {
            "request": req,
            "reason": str(reason or "background"),
            "profile_fingerprint": str(profile_fingerprint or ""),
        }
        hint = str(generation_hint or "").strip()
        if not hint:
            hint = f"{int(time.time() // 30)}:{_digest(payload)}"
        generation_key = f"{HomeJobType.REFRESH_CANDIDATES.value}:{hint}"
        priority = _supply_priority(req, reason, 90)
        if coalesce_terminal and priority < 110:
            return self._repositories.jobs.enqueue_coalesced(
                scope,
                HomeJobType.REFRESH_CANDIDATES,
                generation_key,
                payload=payload,
                priority=priority,
                max_attempts=4,
                run_after=run_after,
            )
        return self._repositories.jobs.enqueue_singleton(
            scope,
            HomeJobType.REFRESH_CANDIDATES,
            generation_key,
            payload=payload,
            priority=priority,
            max_attempts=4,
            run_after=run_after,
            expedite=priority >= 110,
        )

    def radio(
        self,
        request: Any,
        *,
        reason: str,
        profile_fingerprint: str = "",
        candidate_snapshot_id: str = "",
        generation_hint: str = "",
        coalesce_terminal: bool = False,
        run_after: float | None = None,
    ) -> HomeJobRecord:
        req = request_payload(request)
        scope = str(req.get("user_scope_id") or "guest").strip() or "guest"
        payload = {
            "request": req,
            "reason": str(reason or "background"),
            "profile_fingerprint": str(profile_fingerprint or ""),
            "candidate_snapshot_id": str(candidate_snapshot_id or ""),
        }
        hint = str(generation_hint or "").strip()
        if not hint:
            hint = f"{int(time.time() // 30)}:{_digest(payload)}"
        priority = _supply_priority(req, reason, 95)
        enqueue = (
            self._repositories.jobs.enqueue_coalesced
            if coalesce_terminal and priority < 110
            else self._repositories.jobs.enqueue_singleton
        )
        return enqueue(
            scope,
            HomeJobType.REFRESH_RADIO,
            f"{HomeJobType.REFRESH_RADIO.value}:{hint}",
            payload=payload,
            # A ready radio reservoir unlocks local feed composition and must
            # not sit behind optional candidate enrichment.
            priority=priority,
            max_attempts=6,
            run_after=run_after,
            **({"expedite": True} if priority >= 110 else {}),
        )

    def artwork(
        self,
        request: Any,
        record: Mapping[str, Any],
        *,
        profile_fingerprint: str = "",
    ) -> HomeJobRecord:
        identity = str(
            record.get("canonical_artist_id")
            or record.get("provider_artist_id")
            or record.get("id")
            or record.get("name")
            or ""
        ).strip().casefold()
        if not identity:
            raise ValueError("artwork repair requires an artist identity")
        # Identity, rather than the mutable artwork payload, is the durable
        # coalescing key.  A retry/cooldown generation can therefore never be
        # multiplied merely because a provider returned a different URL.
        cooldown = int(time.time() // (8 * 60 * 60))
        req = request_payload(request)
        scope = str(req.get("user_scope_id") or "guest").strip() or "guest"
        payload = {"request": req, "reason": "radio_artwork_repair",
                   "profile_fingerprint": str(profile_fingerprint or ""),
                   "artist": dict(record)}
        return self._repositories.jobs.enqueue_coalesced(
            scope, HomeJobType.REPAIR_ARTWORK, f"repair_artwork:{identity}:{cooldown}",
            payload=payload, priority=96, max_attempts=3,
        )

    def compose(
        self,
        request: Any,
        *,
        reason: str,
        profile_fingerprint: str = "",
        candidate_snapshot_id: str = "",
        radio_snapshot_id: str = "",
        allocation_epoch: int = 0,
        generation_hint: str = "",
        run_after: float | None = None,
    ) -> HomeJobRecord:
        del generation_hint  # Queue-slot demand, not caller intent, owns idempotency.
        candidate_id = str(candidate_snapshot_id or "").strip()
        radio_id = str(radio_snapshot_id or "").strip()
        if not candidate_id:
            raise ValueError("feed composition requires an exact candidate snapshot")
        req = request_payload(request)
        scope = str(req.get("user_scope_id") or "guest").strip() or "guest"
        state = self._repositories.state.reconcile(scope)
        epoch = max(int(allocation_epoch or 0), 0)
        if epoch <= 0:
            epoch = state.active_version + len(state.ready_artifact_ids) + 1
        payload = {
            "request": req,
            "reason": str(reason or "background"),
            "profile_fingerprint": str(profile_fingerprint or ""),
            "candidate_snapshot_id": candidate_id,
            "radio_snapshot_id": radio_id,
            "allocation_epoch": epoch,
        }
        # Queue demand, not every intermediate supply revision, owns compose
        # work. A running/pending slot consumes the newest usable snapshots
        # when claimed, so candidate/radio enrichment cannot create a compose
        # storm for the same missing feed.
        return self._repositories.jobs.enqueue_singleton(
            scope,
            HomeJobType.COMPOSE_FEED,
            f"compose:slot:{epoch}:state:{max(int(getattr(state, 'revision', 0) or 0), 0)}",
            payload=payload,
            priority=100,
            max_attempts=4,
            run_after=run_after,
        )
