from __future__ import annotations

import pathlib
import json
import sqlite3
import sys
import time
from types import SimpleNamespace


CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.home_feed import (  # noqa: E402
    ACTIVE_ARTIFACT_TTL_SECONDS,
    CANDIDATE_SNAPSHOT_TTL_SECONDS,
    RADIO_SNAPSHOT_TTL_SECONDS,
    CandidateSnapshotRecord,
    FeedArtifactRecord,
    HomeJobStatus,
    HomeJobType,
    HomeRepositorySet,
    RadioSnapshotRecord,
    evaluate_feed_contract,
    home_supply_profile_fingerprint,
)
from auralis_backend.discovery.inventory import (  # noqa: E402
    CandidateInventory,
    candidate_inventory_to_payload,
)
from auralis_backend.discovery.config import ARTIFACT_VERSION  # noqa: E402
from auralis_backend.discovery.schema import DiscoveryRow, TasteProfile  # noqa: E402
from auralis_backend.home_feed.api import HomeFeedApi  # noqa: E402
from auralis_backend.home_feed.composer import HomeFeedComposer  # noqa: E402
from auralis_backend.home_feed.bundles import with_feed_bundle_manifest  # noqa: E402
from auralis_backend.home_feed.scheduler import HomeJobScheduler  # noqa: E402
from auralis_backend.home_feed.worker import HomeFeedWorker  # noqa: E402


def test_compose_scheduler_coalesces_supply_updates_for_one_queue_slot(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "compose-slot.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    scheduler = HomeJobScheduler(object())

    jobs = [
        scheduler.compose(
            {"user_scope_id": "user-1"},
            reason="supply_advanced",
            candidate_snapshot_id=f"candidate-{index}",
            radio_snapshot_id=f"radio-{index}",
            allocation_epoch=1,
        )
        for index in range(12)
    ]

    assert len({job.job_id for job in jobs}) == 1
    actionable = [
        job
        for job in repositories.jobs.list_for_scope("user-1")
        if job.job_type is HomeJobType.COMPOSE_FEED
        and job.status in {
            HomeJobStatus.PENDING,
            HomeJobStatus.RETRY,
            HomeJobStatus.RUNNING,
        }
    ]
    assert len(actionable) == 1
    assert actionable[0].generation_key.startswith("compose:slot:1:state:")


def test_api_never_schedules_composition_from_a_legacy_profile_fragment(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "stable-cutover.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    now = time.time()
    legacy = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint="legacy-request-profile",
        generated_at=now,
        expires_at=now + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        generation_id="candidate-legacy",
        coverage={"ready": True},
    )
    repositories.candidates.put(
        CandidateSnapshotRecord(
            snapshot_id=legacy.generation_id,
            user_scope_id=legacy.user_scope_id,
            profile_fingerprint=legacy.profile_fingerprint,
            generated_at=legacy.generated_at,
            expires_at=legacy.expires_at,
            payload=candidate_inventory_to_payload(legacy),
        )
    )
    api = HomeFeedApi(object())
    state = repositories.state.get_or_create("user-1")
    request = SimpleNamespace(user_scope_id="user-1")

    api._ensure_successors(request, state, reason="launch_missing_successor")

    jobs = repositories.jobs.list_for_scope("user-1")
    assert any(job.job_type is HomeJobType.REFRESH_CANDIDATES for job in jobs)
    assert not any(job.job_type is HomeJobType.COMPOSE_FEED for job in jobs)
    assert not any(job.job_type is HomeJobType.REFRESH_RADIO for job in jobs)

    stable_profile = home_supply_profile_fingerprint("user-1")
    stable = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint=stable_profile,
        generated_at=now,
        expires_at=now + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        generation_id="candidate-stable",
        coverage={"ready": True},
    )
    repositories.candidates.put(
        CandidateSnapshotRecord(
            snapshot_id=stable.generation_id,
            user_scope_id=stable.user_scope_id,
            profile_fingerprint=stable.profile_fingerprint,
            generated_at=stable.generated_at,
            expires_at=stable.expires_at,
            payload=candidate_inventory_to_payload(stable),
        )
    )
    repositories.radio.put(
        RadioSnapshotRecord(
            snapshot_id="radio-stable",
            user_scope_id="user-1",
            profile_fingerprint=stable_profile,
            generated_at=now,
            expires_at=now + RADIO_SNAPSHOT_TTL_SECONDS,
            payload={},
        )
    )

    api._ensure_successors(request, state, reason="serve_replenish")

    compose = next(
        job
        for job in repositories.jobs.list_for_scope("user-1")
        if job.job_type is HomeJobType.COMPOSE_FEED
    )
    assert compose.payload["candidate_snapshot_id"] == "candidate-stable"
    assert compose.payload["radio_snapshot_id"] == "radio-stable"


def test_api_allocates_successor_and_replenishes_deficient_reusable_supply(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "api-allocation.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    now = time.time()
    profile = home_supply_profile_fingerprint("user-1")
    inventory = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint=profile,
        generated_at=now,
        expires_at=now + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        generation_id="candidate-shared",
        coverage={"ready": True},
    )
    repositories.candidates.put(
        CandidateSnapshotRecord(
            snapshot_id=inventory.generation_id,
            user_scope_id=inventory.user_scope_id,
            profile_fingerprint=inventory.profile_fingerprint,
            generated_at=inventory.generated_at,
            expires_at=inventory.expires_at,
            payload=candidate_inventory_to_payload(inventory),
        )
    )
    active = _artifact(
        "feed-active",
        candidate_snapshot_id=inventory.generation_id,
        radio_snapshot_id="",
        created_at=now,
    )
    repositories.artifacts.put(active)
    state = repositories.state.get_or_create("user-1")
    state = repositories.state.initialize_active(
        "user-1",
        active.artifact_id,
        expected_revision=state.revision,
    )

    api = HomeFeedApi(object())
    api._ensure_successors(
        SimpleNamespace(user_scope_id="user-1"),
        state,
        reason="post_promotion",
    )

    jobs = repositories.jobs.list_for_scope("user-1")
    compose = next(job for job in jobs if job.job_type is HomeJobType.COMPOSE_FEED)
    assert compose.payload["candidate_snapshot_id"] == inventory.generation_id
    assert compose.payload["allocation_epoch"] == 2
    diagnostics = api._queue_diagnostics(state)
    assert diagnostics["ready_feed_depth"] == 0
    assert diagnostics["queue_phase"] == "building"
    assert diagnostics["queue_build_inflight"] is True
    replenishment = next(
        job for job in jobs if job.job_type is HomeJobType.REFRESH_CANDIDATES
    )
    assert replenishment.payload["reason"] == "candidate_reserve_replenish"


def test_api_serves_expired_usable_supply_and_schedules_one_revalidation(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "api-revalidation.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    now = time.time()
    profile = home_supply_profile_fingerprint("user-1")
    inventory = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint=profile,
        generated_at=now - CANDIDATE_SNAPSHOT_TTL_SECONDS - 60,
        expires_at=now - 60,
        generation_id="candidate-expired-usable",
        coverage={"ready": True},
    )
    repositories.candidates.put(
        CandidateSnapshotRecord(
            snapshot_id=inventory.generation_id,
            user_scope_id=inventory.user_scope_id,
            profile_fingerprint=inventory.profile_fingerprint,
            generated_at=inventory.generated_at,
            expires_at=inventory.expires_at,
            payload=candidate_inventory_to_payload(inventory),
        )
    )
    active = _artifact(
        "feed-active-stale-supply",
        candidate_snapshot_id=inventory.generation_id,
        radio_snapshot_id="",
        created_at=now,
    )
    repositories.artifacts.put(active)
    state = repositories.state.get_or_create("user-1")
    state = repositories.state.initialize_active(
        "user-1",
        active.artifact_id,
        expected_revision=state.revision,
    )
    api = HomeFeedApi(object())
    request = SimpleNamespace(user_scope_id="user-1")

    api._ensure_successors(request, state, reason="serve_replenish")
    api._ensure_successors(request, state, reason="serve_replenish")

    jobs = repositories.jobs.list_for_scope("user-1")
    candidate_jobs = [
        job for job in jobs if job.job_type is HomeJobType.REFRESH_CANDIDATES
    ]
    compose_jobs = [job for job in jobs if job.job_type is HomeJobType.COMPOSE_FEED]
    assert len(candidate_jobs) == 1
    assert candidate_jobs[0].payload["reason"] == "candidate_revalidation_due"
    assert len(compose_jobs) == 1
    assert compose_jobs[0].payload["candidate_snapshot_id"] == inventory.generation_id


def test_candidate_revalidation_is_coalesced_for_the_completed_retry_window(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "refresh-window.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    scheduler = HomeJobScheduler(object())
    request = SimpleNamespace(user_scope_id="user-1")

    first = scheduler.candidates(
        request,
        reason="candidate_revalidation_due",
        generation_hint="revalidate:candidate-1:window-1",
        coalesce_terminal=True,
    )
    claimed = repositories.jobs.claim_next("worker-1")
    assert claimed is not None and claimed.job_id == first.job_id
    repositories.jobs.complete(claimed.job_id, claimed.lease_token)

    second = scheduler.candidates(
        request,
        reason="candidate_revalidation_due",
        generation_hint="revalidate:candidate-1:window-1",
        coalesce_terminal=True,
    )

    assert second.job_id == first.job_id
    assert second.status is HomeJobStatus.SUCCEEDED
    assert len(repositories.jobs.list_for_scope("user-1")) == 1


def _factory(path: pathlib.Path):
    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    return connect


def _rows(generation: str) -> list[dict]:
    def row(kind: str, count: int) -> dict:
        return {
            "kind": kind,
            "items": [
                {"id": f"{generation}:{kind}:{index}"} for index in range(count)
            ],
        }

    return [
        row("todays_pick", 6),
        row("featured_new_albums", 8),
        row("made_for_you", 8),
        row("popular_radio", 12),
        row("recommended_albums", 12),
        row("recommended_artists", 10),
        row("quiet_picks", 20),
    ]


def _artifact(
    artifact_id: str,
    *,
    candidate_snapshot_id: str,
    radio_snapshot_id: str,
    created_at: float,
) -> FeedArtifactRecord:
    rows = _rows(artifact_id)
    assert evaluate_feed_contract(rows).accepted is True
    return FeedArtifactRecord(
        artifact_id=artifact_id,
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        candidate_snapshot_id=candidate_snapshot_id,
        radio_snapshot_id=radio_snapshot_id,
        created_at=created_at,
        expires_at=created_at + ACTIVE_ARTIFACT_TTL_SECONDS,
        payload=with_feed_bundle_manifest(
            {"rows": rows, "accepted": True, "diagnostics": {}}
        ),
    )


def test_persisted_usage_cycle_survives_restart_and_replenishes_fifo(
    tmp_path: pathlib.Path,
) -> None:
    factory = _factory(tmp_path / "home-lifecycle.sqlite")
    repositories = HomeRepositorySet(factory)
    now = 1_000.0
    candidates = []
    radios = []
    for index in range(4):
        candidate = CandidateSnapshotRecord(
            snapshot_id=f"candidate-{index}",
            user_scope_id="user-1",
            profile_fingerprint="profile-1",
            generated_at=now - index,
            expires_at=now - index + CANDIDATE_SNAPSHOT_TTL_SECONDS,
            payload={"canonical_track_ids": [f"track-{index}-{value}" for value in range(100)]},
        )
        radio = RadioSnapshotRecord(
            snapshot_id=f"radio-{index}",
            user_scope_id="user-1",
            profile_fingerprint="profile-1",
            generated_at=now + index,
            expires_at=now + index + RADIO_SNAPSHOT_TTL_SECONDS,
            payload={"rotation_ids": [f"radio-{index}"]},
        )
        repositories.candidates.put(candidate)
        repositories.radio.put(radio)
        candidates.append(candidate)
        radios.append(radio)

    for index, artifact_id in enumerate(("feed-a", "feed-b", "feed-c")):
        repositories.artifacts.put(
            _artifact(
                artifact_id,
                candidate_snapshot_id=candidates[index].snapshot_id,
                radio_snapshot_id=radios[index].snapshot_id,
                created_at=now + index,
            )
        )

    state = repositories.state.get_or_create("user-1", now=now)
    state = repositories.state.initialize_active(
        "user-1", "feed-a", expected_revision=state.revision, now=now
    )
    state = repositories.state.append_ready(
        "user-1", "feed-b", expected_revision=state.revision, now=now + 1
    )
    state = repositories.state.append_ready(
        "user-1", "feed-c", expected_revision=state.revision, now=now + 2
    )
    assert state.ready_artifact_ids == ("feed-b", "feed-c")

    # Reconstruct every repository to mimic a backend process restart.
    restarted = HomeRepositorySet(factory)
    state = restarted.state.get("user-1")
    assert state is not None
    state, promoted = restarted.state.promote_ready(
        "user-1", expected_revision=state.revision, now=now + 3
    )
    assert promoted == "feed-b"
    assert restarted.artifacts.get(promoted) is not None
    assert state.ready_artifact_ids == ("feed-c",)

    replenish = restarted.jobs.enqueue(
        "user-1",
        HomeJobType.COMPOSE_FEED,
        "candidate-3:radio-3:slot-2",
        payload={"parent_artifact_id": "feed-c"},
        now=now + 3,
    )
    claimed = restarted.jobs.claim_next("home-worker", now=now + 3)
    assert claimed is not None and claimed.job_id == replenish.job_id

    restarted.artifacts.put(
        _artifact(
            "feed-d",
            candidate_snapshot_id=candidates[3].snapshot_id,
            radio_snapshot_id=radios[3].snapshot_id,
            created_at=now + 4,
        )
    )
    state = restarted.state.append_ready(
        "user-1", "feed-d", expected_revision=state.revision, now=now + 4
    )
    completed = restarted.jobs.complete(claimed.job_id, claimed.lease_token, now=now + 4)
    assert completed.status is HomeJobStatus.SUCCEEDED
    assert state.ready_artifact_ids == ("feed-c", "feed-d")

    state, promoted = restarted.state.promote_ready(
        "user-1", expected_revision=state.revision, now=now + 5
    )
    assert promoted == "feed-c"
    assert state.active_artifact_id == "feed-c"
    assert state.ready_artifact_ids == ("feed-d",)


def test_candidate_supply_expires_for_refresh_but_remains_usable(
    tmp_path: pathlib.Path,
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "home-expiry.sqlite"))
    snapshot = CandidateSnapshotRecord(
        snapshot_id="candidate-expired",
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=100,
        expires_at=100 + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        payload={"canonical_track_ids": ["track-1"]},
    )
    repositories.candidates.put(snapshot)

    assert repositories.candidates.get(snapshot.snapshot_id) == snapshot
    assert repositories.candidates.latest_fresh(
        "user-1", now=snapshot.expires_at + 1
    ) is None
    assert repositories.candidates.latest_usable(
        "user-1", now=snapshot.expires_at + 1
    ) == snapshot

    refresh = repositories.jobs.enqueue(
        "user-1",
        HomeJobType.REFRESH_CANDIDATES,
        "candidate-after-expiry",
        payload={"expired_snapshot_id": snapshot.snapshot_id},
        now=snapshot.expires_at + 1,
    )
    claimed = repositories.jobs.claim_next(
        "candidate-worker", now=snapshot.expires_at + 1
    )
    assert claimed is not None
    assert claimed.job_id == refresh.job_id
    assert claimed.payload["expired_snapshot_id"] == snapshot.snapshot_id


def _taste() -> TasteProfile:
    return TasteProfile(
        user_scope_id="user-1",
        profile_key="profile-1",
        signal_tier="established",
        artist_hints=["Artist One"],
    )


def _ranked_core_rows(epoch: int) -> list[DiscoveryRow]:
    def row(kind: str, count: int) -> DiscoveryRow:
        return DiscoveryRow(
            id=kind,
            title=kind,
            kind=kind,
            item_type="track",
            items=[{"id": f"epoch-{epoch}:{kind}:{index}"} for index in range(count)],
        )

    return [
        row("todays_pick", 6),
        row("featured_new_albums", 8),
        row("made_for_you", 8),
        row("popular_radio", 12),
        row("recommended_albums", 12),
        row("recommended_artists", 10),
        row("quiet_picks", 20),
    ]


def test_pure_composer_allocates_active_and_ready_feeds_from_one_supply(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "composer.sqlite"))
    now = time.time()
    inventories = []
    for index in range(3):
        inventory = CandidateInventory(
            user_scope_id="user-1",
            profile_fingerprint="profile-1",
            generated_at=now - index,
            expires_at=now - index + CANDIDATE_SNAPSHOT_TTL_SECONDS,
            generation_id=f"candidate-generation-{index}",
            coverage={"ready": True},
        )
        repositories.candidates.put(
            CandidateSnapshotRecord(
                snapshot_id=inventory.generation_id,
                user_scope_id=inventory.user_scope_id,
                profile_fingerprint=inventory.profile_fingerprint,
                generated_at=inventory.generated_at,
                expires_at=inventory.expires_at,
                payload=candidate_inventory_to_payload(inventory),
            )
        )
        inventories.append(inventory)
    monkeypatch.setattr(
        "auralis_backend.home_feed.composer.build_rows_from_pools",
        lambda _pools, taste: (
            _ranked_core_rows(taste.rotation_epoch),
            {},
            {},
            {"accepted": True},
        ),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.composer.evaluate_quality",
        lambda **_kwargs: (True, [], "launchable"),
    )
    composer = HomeFeedComposer(repositories)

    first = composer.compose(
        _taste(), reason="usage_cycle", candidate_snapshot_id=inventories[0].generation_id
    )
    first_ready = composer.compose(
        _taste(), reason="usage_cycle", candidate_snapshot_id=inventories[0].generation_id
    )
    successors = [
        composer.compose(
            _taste(),
            reason="usage_cycle",
            candidate_snapshot_id=inventory.generation_id,
        )
        for inventory in inventories[1:]
    ]
    state = repositories.state.get("user-1")

    assert first.reason == "active_initialized"
    assert first_ready.reason == "ready_appended"
    assert [outcome.reason for outcome in successors] == [
        "ready_appended",
        "queue_full",
    ]
    assert state is not None
    assert state.active_artifact_id == first.artifact_id
    assert state.ready_artifact_ids == (
        first_ready.artifact_id,
        successors[0].artifact_id,
    )
    assert len({first.artifact_id, *state.ready_artifact_ids}) == 3


def test_worker_builds_active_plus_two_ready_allocations_without_provider_work(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "worker-cycle.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    now = time.time()
    stable_profile = home_supply_profile_fingerprint("user-1")
    inventory = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint=stable_profile,
        generated_at=now,
        expires_at=now + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        generation_id="candidate-shared",
        coverage={"ready": True},
    )
    repositories.candidates.put(
        CandidateSnapshotRecord(
            snapshot_id=inventory.generation_id,
            user_scope_id=inventory.user_scope_id,
            profile_fingerprint=inventory.profile_fingerprint,
            generated_at=inventory.generated_at,
            expires_at=inventory.expires_at,
            payload=candidate_inventory_to_payload(inventory),
        )
    )
    stable_taste = _taste()
    stable_taste.profile_key = stable_profile
    monkeypatch.setattr(
        "auralis_backend.home_feed.worker.build_taste_profile",
        lambda _server, _request: stable_taste,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.composer.build_rows_from_pools",
        lambda _pools, taste: (
            _ranked_core_rows(taste.rotation_epoch),
            {},
            {},
            {"accepted": True},
        ),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.composer.evaluate_quality",
        lambda **_kwargs: (True, [], "launchable"),
    )
    HomeJobScheduler(object()).compose(
        {"user_scope_id": "user-1"},
        reason="usage_cycle",
        profile_fingerprint=stable_profile,
        candidate_snapshot_id=inventory.generation_id,
    )
    worker = HomeFeedWorker(object(), worker_id="usage-cycle-worker")
    worker._candidate_supply.refresh = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("candidate provider work entered feed allocation")
    )
    worker._radio_supply.refresh = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("radio provider work entered feed allocation")
    )

    api = HomeFeedApi(object())
    results = [worker.run_once()]
    after_active = repositories.state.get("user-1")
    assert after_active is not None
    active_diagnostics = api._queue_diagnostics(after_active)
    assert active_diagnostics["ready_feed_depth"] == 0
    assert active_diagnostics["queue_phase"] == "building"

    results.append(worker.run_once())
    after_first_ready = repositories.state.get("user-1")
    assert after_first_ready is not None
    first_ready_diagnostics = api._queue_diagnostics(after_first_ready)
    assert first_ready_diagnostics["ready_feed_depth"] == 1
    assert first_ready_diagnostics["queue_phase"] == "building"

    results.append(worker.run_once())
    state = repositories.state.get("user-1")

    assert len(results) == 3
    assert state is not None
    assert state.active_artifact_id
    assert len(state.ready_artifact_ids) == 2
    records = [
        repositories.artifacts.get(artifact_id)
        for artifact_id in (state.active_artifact_id, *state.ready_artifact_ids)
    ]
    assert all(record is not None for record in records)
    assert {record.candidate_snapshot_id for record in records if record} == {
        inventory.generation_id
    }
    assert [record.allocation_epoch for record in records if record] == [1, 2, 3]
    diagnostics = api._queue_diagnostics(state)
    assert diagnostics["ready_feed_depth"] == 2
    assert diagnostics["queue_phase"] == "ready"


def test_composer_never_uses_a_snapshot_from_an_old_profile(
    tmp_path: pathlib.Path,
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "profile.sqlite"))
    now = time.time()
    old_inventory = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint="old-profile",
        generated_at=now,
        expires_at=now + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        generation_id="old-profile-candidates",
        coverage={"ready": True},
    )
    repositories.candidates.put(
        CandidateSnapshotRecord(
            snapshot_id=old_inventory.generation_id,
            user_scope_id=old_inventory.user_scope_id,
            profile_fingerprint=old_inventory.profile_fingerprint,
            generated_at=old_inventory.generated_at,
            expires_at=old_inventory.expires_at,
            payload=candidate_inventory_to_payload(old_inventory),
        )
    )

    outcome = HomeFeedComposer(repositories).compose(
        _taste(), reason="profile_changed"
    )

    assert outcome.reason == "candidate_snapshot_missing"
    assert repositories.state.get("user-1") is not None


def test_home_api_promotes_fifo_once_and_serves_rows_from_new_repository(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "api.sqlite"))
    now = time.time()
    records = []
    for index, artifact_id in enumerate(("feed-a", "feed-b", "feed-c")):
        candidate = CandidateSnapshotRecord(
            snapshot_id=f"candidate-api-{index}",
            user_scope_id="user-1",
            profile_fingerprint="profile-1",
            generated_at=now + index,
            expires_at=now + index + CANDIDATE_SNAPSHOT_TTL_SECONDS,
            payload={"inventory_version": "unused-by-api"},
        )
        repositories.candidates.put(candidate)
        record = _artifact(
            artifact_id,
            candidate_snapshot_id=candidate.snapshot_id,
            radio_snapshot_id="",
            created_at=now - 10 + index,
        )
        payload = dict(record.payload)
        payload.update(
            {
                "artifact_version": ARTIFACT_VERSION,
                "session_id": artifact_id,
                "user_scope_id": "user-1",
                "profile_key": "profile-1",
                "generated_at": now - 10 + index,
                "expires_at": record.expires_at,
                "diagnostics": {},
                "candidate_pool_counts": {},
                "provider_timings_ms": {},
                "home_tab_lanes": {},
                "accepted": True,
                "quality_reasons": [],
                "artifact_source": "home_snapshot_v2",
            }
        )
        record = FeedArtifactRecord(
            **{
                **record.__dict__,
                "payload": with_feed_bundle_manifest(payload),
            }
        )
        repositories.artifacts.put(record)
        records.append(record)
    state = repositories.state.get_or_create("user-1", now=now)
    state = repositories.state.initialize_active(
        "user-1", "feed-a", expected_revision=state.revision, now=now
    )
    state = repositories.state.append_ready(
        "user-1", "feed-b", expected_revision=state.revision, now=now + 1
    )
    repositories.state.append_ready(
        "user-1", "feed-c", expected_revision=state.revision, now=now + 2
    )
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    server = SimpleNamespace(
        RECOMMENDATION_ROW_PAGE_SIZE=8,
        _recommendation_record_impressions=lambda *_args: None,
    )
    api = HomeFeedApi(server)
    ready_status = api.recommend(
        SimpleNamespace(
            user_scope_id="user-1",
            feed_queue_status_only=True,
            feed_queue_revision=0,
            feed_queue_wait_ms=0,
        ),
        request_mode="queue_status",
    )
    assert ready_status["diagnostics"]["ready_bundle_count"] == 2
    assert ready_status["diagnostics"]["ready_bundles_complete"] is True
    assert (
        ready_status["diagnostics"]["feed_readiness_contract"]
        == "complete_immutable_bundle_v1"
    )
    request = SimpleNamespace(
        user_scope_id="user-1",
        force_refresh=False,
        prefer_fresh_rows=False,
        session_intent=False,
        promote_ready_on_launch=True,
        launch_token="launch-1",
    )

    first = api.recommend(request, request_mode="full_feed")
    second = api.recommend(request, request_mode="full_feed")
    persisted = repositories.state.get("user-1")

    assert first["session_id"] == "feed-b"
    assert first["feed_action"] == "promoted_prepared"
    assert len(first["rows"]) == len(_rows("expected"))
    assert second["session_id"] == "feed-b"
    assert persisted is not None
    assert persisted.active_artifact_id == "feed-b"
    assert persisted.ready_artifact_ids == ("feed-c",)
    assert persisted.last_launch_token == "launch-1"

    page = api.recommend(
        SimpleNamespace(
            user_scope_id="user-1",
            session_id="feed-b",
            row_id="quiet_picks",
            offset=8,
            limit=5,
        ),
        request_mode="row_page",
    )
    assert page["row"]["kind"] == "quiet_picks"
    assert len(page["row"]["items"]) == 5


def test_expired_cold_launch_serves_stale_then_accepts_monotonic_replacement(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    """Exercise the device failure: expired supply, empty active, then recovery."""

    factory = _factory(tmp_path / "expired-cold-launch.sqlite")
    repositories = HomeRepositorySet(factory)
    now = time.time()
    old_created_at = now - ACTIVE_ARTIFACT_TTL_SECONDS - 3600
    old_candidate_generated_at = old_created_at - 60
    old_candidate = CandidateSnapshotRecord(
        snapshot_id="candidate-expired",
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=old_candidate_generated_at,
        expires_at=old_candidate_generated_at + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        payload={"canonical_track_ids": ["old-track"]},
    )
    repositories.candidates.put(old_candidate)

    def complete_artifact(
        artifact_id: str,
        candidate_id: str,
        created_at: float,
    ) -> FeedArtifactRecord:
        base = _artifact(
            artifact_id,
            candidate_snapshot_id=candidate_id,
            radio_snapshot_id="",
            created_at=created_at,
        )
        payload = {
            **dict(base.payload),
            "artifact_version": ARTIFACT_VERSION,
            "session_id": artifact_id,
            "user_scope_id": "user-1",
            "profile_key": "profile-1",
            "generated_at": created_at,
            "expires_at": base.expires_at,
            "candidate_pool_counts": {},
            "provider_timings_ms": {},
            "home_tab_lanes": {},
            "quality_reasons": [],
            "artifact_source": "home_snapshot_v2",
        }
        return FeedArtifactRecord(
            **{**base.__dict__, "payload": with_feed_bundle_manifest(payload)}
        )

    expired = complete_artifact(
        "feed-expired", old_candidate.snapshot_id, old_created_at
    )
    repositories.artifacts.put(expired)
    state = repositories.state.get_or_create("user-1", now=old_created_at)
    state = repositories.state.initialize_active(
        "user-1",
        expired.artifact_id,
        expected_revision=state.revision,
        now=old_created_at + 1,
    )
    connection = factory()
    try:
        connection.execute(
            "UPDATE home_feed_state SET active_version = 68 WHERE user_scope_id = ?",
            ["user-1"],
        )
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    api = HomeFeedApi(
        SimpleNamespace(
            RECOMMENDATION_ROW_PAGE_SIZE=8,
            _recommendation_record_impressions=lambda *_args: None,
        )
    )
    request = SimpleNamespace(
        user_scope_id="user-1",
        profile_key="profile-1",
        force_refresh=False,
        prefer_fresh_rows=False,
        session_intent=False,
        promote_ready_on_launch=True,
        launch_token="expired-launch",
    )

    stale_response = api.recommend(request, request_mode="full_feed")
    reconciled = repositories.state.get("user-1")

    assert stale_response["session_id"] == expired.artifact_id
    assert stale_response["rows"]
    assert stale_response["diagnostics"]["artifact_status"] == "stale_fallback"
    assert reconciled is not None
    assert reconciled.active_artifact_id == ""
    assert reconciled.active_version == 68
    assert sum(
        job.job_type is HomeJobType.REFRESH_CANDIDATES
        and job.status in {
            HomeJobStatus.PENDING,
            HomeJobStatus.RETRY,
            HomeJobStatus.RUNNING,
        }
        for job in repositories.jobs.list_for_scope("user-1")
    ) == 1

    fresh_candidate = CandidateSnapshotRecord(
        snapshot_id="candidate-fresh",
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=now,
        expires_at=now + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        payload={"canonical_track_ids": ["fresh-track"]},
    )
    repositories.candidates.put(fresh_candidate)
    fresh = complete_artifact("feed-fresh", fresh_candidate.snapshot_id, now)
    repositories.artifacts.put(fresh)
    state = repositories.state.get("user-1")
    assert state is not None
    state = repositories.state.initialize_active(
        "user-1", fresh.artifact_id, expected_revision=state.revision, now=now
    )

    assert state.active_version == 69
    fresh_response = api.recommend(
        SimpleNamespace(
            **{
                **vars(request),
                "promote_ready_on_launch": False,
                "launch_token": "",
            }
        ),
        request_mode="full_feed",
    )
    assert fresh_response["session_id"] == fresh.artifact_id
    assert fresh_response["rows"]
    assert fresh_response["diagnostics"]["artifact_status"] == "servable"


def test_composer_uses_pinned_candidate_generation_across_profile_drift(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "pinned-compose.sqlite"))
    now = time.time()
    inventory = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-pinned",
        generated_at=now,
        expires_at=now + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        generation_id="candidate-pinned",
        coverage={"ready": True},
    )
    repositories.candidates.put(
        CandidateSnapshotRecord(
            snapshot_id=inventory.generation_id,
            user_scope_id=inventory.user_scope_id,
            profile_fingerprint=inventory.profile_fingerprint,
            generated_at=inventory.generated_at,
            expires_at=inventory.expires_at,
            payload=candidate_inventory_to_payload(inventory),
        )
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.composer.build_rows_from_pools",
        lambda _pools, taste: (
            _ranked_core_rows(taste.rotation_epoch),
            {},
            {},
            {"accepted": True},
        ),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.composer.evaluate_quality",
        lambda **_kwargs: (True, [], "launchable"),
    )

    outcome = HomeFeedComposer(repositories).compose(
        TasteProfile(
            user_scope_id="user-1",
            profile_key="profile-from-later-request",
            signal_tier="established",
        ),
        reason="pinned-generation",
        candidate_snapshot_id=inventory.generation_id,
    )
    artifact = repositories.artifacts.get(outcome.artifact_id)

    assert outcome.reason == "active_initialized"
    assert artifact is not None
    assert artifact.candidate_snapshot_id == inventory.generation_id
    assert artifact.profile_fingerprint == "profile-pinned"
