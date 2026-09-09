from __future__ import annotations

import pathlib
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest


CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.home_feed import (  # noqa: E402
    CANDIDATE_SNAPSHOT_TTL_SECONDS,
    RADIO_SNAPSHOT_TTL_SECONDS,
    ACTIVE_ARTIFACT_TTL_SECONDS,
    CandidateSnapshotRecord,
    FeedArtifactRecord,
    HomeJobStatus,
    HomeJobType,
    HomeJobScheduler,
    HomeRepositorySet,
    ImmutableRecordConflict,
    InvalidArtifactReference,
    LeaseLost,
    QueueFull,
    RadioSnapshotRecord,
    StaleRevision,
)
from auralis_backend.home_feed.bundles import with_feed_bundle_manifest  # noqa: E402


def _factory(path: pathlib.Path):
    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    return connect


def _seed_artifacts(repositories: HomeRepositorySet, *, now: float = 100) -> None:
    candidate = CandidateSnapshotRecord(
        snapshot_id="candidate-state",
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=now,
        expires_at=now + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        payload={"track_ids": ["track-1"]},
    )
    repositories.candidates.put(candidate)
    rows = [
        {"kind": "todays_pick", "items": [{"id": f"today-{i}"} for i in range(6)]},
        {"kind": "featured_new_albums", "items": [{"id": f"featured-{i}"} for i in range(8)]},
        {"kind": "made_for_you", "items": [{"id": f"mix-{i}"} for i in range(8)]},
        {"kind": "popular_radio", "items": [{"id": f"radio-{i}"} for i in range(12)]},
        {"kind": "recommended_albums", "items": [{"id": f"album-{i}"} for i in range(12)]},
        {"kind": "recommended_artists", "items": [{"id": f"artist-{i}"} for i in range(10)]},
        {"kind": "quiet_picks", "items": [{"id": f"quiet-{i}"} for i in range(20)]},
    ]
    for index, artifact_id in enumerate(("feed-a", "feed-b", "feed-c", "feed-d")):
        radio = RadioSnapshotRecord(
            snapshot_id=f"radio-state-{index}",
            user_scope_id="user-1",
            profile_fingerprint="profile-1",
            generated_at=now + index,
            expires_at=now + index + RADIO_SNAPSHOT_TTL_SECONDS,
            payload={"rotation": index},
        )
        repositories.radio.put(radio)
        repositories.artifacts.put(
            FeedArtifactRecord(
                artifact_id=artifact_id,
                user_scope_id="user-1",
                profile_fingerprint="profile-1",
                candidate_snapshot_id=candidate.snapshot_id,
                radio_snapshot_id=radio.snapshot_id,
                created_at=now + index,
                expires_at=now + index + ACTIVE_ARTIFACT_TTL_SECONDS,
                payload=with_feed_bundle_manifest(
                    {"rows": rows, "accepted": True, "diagnostics": {}}
                ),
            )
        )


def test_schema_is_first_class_and_idempotent(tmp_path: pathlib.Path) -> None:
    factory = _factory(tmp_path / "home.sqlite")
    HomeRepositorySet(factory)
    HomeRepositorySet(factory)

    connection = factory()
    try:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "home_feed_schema_meta",
            "home_feed_state",
            "home_feed_jobs",
            "home_candidate_snapshots",
            "home_radio_snapshots",
            "home_feed_artifacts",
            "home_feed_exposures",
        }.issubset(tables)
        version = connection.execute(
            "SELECT version FROM home_feed_schema_meta WHERE singleton = 1"
        ).fetchone()
        assert version["version"] == 7
    finally:
        connection.close()


def test_delayed_idle_supply_does_not_own_an_urgent_refresh(
    tmp_path: pathlib.Path,
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "idle-supply.sqlite"))
    now = 1_000.0
    idle = repositories.jobs.enqueue_singleton(
        "user-1",
        HomeJobType.REFRESH_CANDIDATES,
        "refresh_candidates:idle:1",
        payload={
            "request": {"user_scope_id": "user-1"},
            "reason": "idle_reserve_enrichment",
        },
        run_after=now + 1_800,
        now=now,
    )

    assert repositories.jobs.has_active(
        "user-1",
        job_types=(HomeJobType.REFRESH_CANDIDATES,),
        now=now,
    ) is True
    assert repositories.jobs.has_active(
        "user-1",
        job_types=(HomeJobType.REFRESH_CANDIDATES,),
        ignore_delayed_idle=True,
        now=now,
    ) is False

    urgent = repositories.jobs.enqueue_singleton(
        "user-1",
        HomeJobType.REFRESH_CANDIDATES,
        "refresh_candidates:urgent:1",
        payload={
            "request": {"user_scope_id": "user-1"},
            "reason": "candidate_revalidation_due",
        },
        now=now + 1,
    )

    assert urgent.job_id != idle.job_id
    assert repositories.jobs.get(idle.job_id).status is HomeJobStatus.CANCELLED
    assert urgent.status is HomeJobStatus.PENDING


def test_schema_migrates_existing_artifacts_to_allocation_epochs(
    tmp_path: pathlib.Path,
) -> None:
    factory = _factory(tmp_path / "home-v6.sqlite")
    connection = factory()
    try:
        connection.execute(
            """
            CREATE TABLE home_feed_artifacts (
                artifact_id TEXT PRIMARY KEY,
                user_scope_id TEXT NOT NULL,
                profile_fingerprint TEXT NOT NULL DEFAULT '',
                candidate_snapshot_id TEXT NOT NULL,
                radio_snapshot_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                payload_json TEXT NOT NULL,
                content_hash TEXT NOT NULL
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    HomeRepositorySet(factory)

    connection = factory()
    try:
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(home_feed_artifacts)"
            ).fetchall()
        }
        assert "allocation_epoch" in columns
    finally:
        connection.close()


def test_state_repository_is_cas_guarded_persistent_fifo(tmp_path: pathlib.Path) -> None:
    factory = _factory(tmp_path / "home.sqlite")
    repositories = HomeRepositorySet(factory)
    _seed_artifacts(repositories)
    repository = repositories.state
    state = repository.get_or_create("user-1", now=100)
    state = repository.initialize_active(
        "user-1", "feed-a", expected_revision=state.revision, now=101
    )
    state = repository.append_ready(
        "user-1", "feed-b", expected_revision=state.revision, now=102
    )
    stale_revision = state.revision
    state = repository.append_ready(
        "user-1", "feed-c", expected_revision=state.revision, now=103
    )
    assert state.ready_artifact_ids == ("feed-b", "feed-c")

    with pytest.raises(QueueFull):
        repository.append_ready(
            "user-1", "feed-d", expected_revision=state.revision, now=104
        )
    with pytest.raises(StaleRevision):
        repository.promote_ready("user-1", expected_revision=stale_revision, now=104)

    restarted = HomeRepositorySet(factory).state
    reloaded = restarted.get("user-1")
    assert reloaded is not None
    promoted_state, promoted = restarted.promote_ready(
        "user-1", expected_revision=reloaded.revision, now=105
    )
    assert promoted == "feed-b"
    assert promoted_state.active_artifact_id == "feed-b"
    assert promoted_state.ready_artifact_ids == ("feed-c",)
    assert promoted_state.active_version == 2


def test_concurrent_queue_appends_have_one_transactional_winner(
    tmp_path: pathlib.Path,
) -> None:
    factory = _factory(tmp_path / "home.sqlite")
    repositories = HomeRepositorySet(factory)
    _seed_artifacts(repositories)
    state = repositories.state.get_or_create("user-1", now=100)
    state = repositories.state.initialize_active(
        "user-1", "feed-a", expected_revision=state.revision, now=101
    )
    barrier = threading.Barrier(2)

    def append(artifact_id: str) -> str:
        barrier.wait(timeout=5)
        try:
            repositories.state.append_ready(
                "user-1",
                artifact_id,
                expected_revision=state.revision,
                now=102,
            )
            return "stored"
        except StaleRevision:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(append, ("feed-b", "feed-c")))
    assert sorted(outcomes) == ["stale", "stored"]
    persisted = repositories.state.get("user-1")
    assert persisted is not None and len(persisted.ready_artifact_ids) == 1


def test_expired_ready_artifact_cannot_be_promoted(tmp_path: pathlib.Path) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "home.sqlite"))
    _seed_artifacts(repositories)
    state = repositories.state.get_or_create("user-1", now=100)
    state = repositories.state.initialize_active(
        "user-1", "feed-a", expected_revision=state.revision, now=101
    )
    state = repositories.state.append_ready(
        "user-1", "feed-b", expected_revision=state.revision, now=102
    )
    with pytest.raises(InvalidArtifactReference, match="promotion TTL"):
        repositories.state.promote_ready(
            "user-1",
            expected_revision=state.revision,
            now=102 + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        )


def test_candidate_snapshots_distinguish_fresh_from_durable_usable_supply(
    tmp_path: pathlib.Path,
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "home.sqlite"))
    snapshot = CandidateSnapshotRecord(
        snapshot_id="candidate-1",
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=100,
        expires_at=100 + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        payload={"track_ids": ["a", "b"]},
    )
    repositories.candidates.put(snapshot)
    repositories.candidates.put(snapshot)
    assert repositories.candidates.latest_fresh("user-1", now=101) == snapshot
    assert repositories.candidates.latest_fresh(
        "user-1", now=snapshot.expires_at
    ) is None
    assert repositories.candidates.latest_usable(
        "user-1", now=snapshot.expires_at
    ) == snapshot

    with pytest.raises(ImmutableRecordConflict):
        repositories.candidates.put(
            CandidateSnapshotRecord(
                snapshot_id=snapshot.snapshot_id,
                user_scope_id=snapshot.user_scope_id,
                profile_fingerprint=snapshot.profile_fingerprint,
                generated_at=snapshot.generated_at,
                expires_at=snapshot.expires_at,
                payload={"track_ids": ["changed"]},
            )
        )


def test_artifacts_require_existing_supply_and_are_immutable(tmp_path: pathlib.Path) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "home.sqlite"))
    missing = FeedArtifactRecord(
        artifact_id="feed-missing-supply",
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        candidate_snapshot_id="candidate-missing",
        radio_snapshot_id="",
        created_at=100,
        expires_at=100 + ACTIVE_ARTIFACT_TTL_SECONDS,
        payload={"rows": []},
    )
    with pytest.raises(InvalidArtifactReference):
        repositories.artifacts.put(missing)

    _seed_artifacts(repositories)
    incomplete_bundle = FeedArtifactRecord(
        artifact_id="feed-incomplete-bundle",
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        candidate_snapshot_id="candidate-state",
        radio_snapshot_id="",
        created_at=100,
        expires_at=100 + ACTIVE_ARTIFACT_TTL_SECONDS,
        payload={"rows": [{"id": "row-1", "items": []}], "accepted": True},
    )
    with pytest.raises(InvalidArtifactReference, match="complete immutable bundle"):
        repositories.artifacts.put(incomplete_bundle)

    stored = repositories.artifacts.get("feed-a")
    assert stored is not None
    repositories.artifacts.put(stored)
    with pytest.raises(ImmutableRecordConflict):
        repositories.artifacts.put(
            FeedArtifactRecord(
                artifact_id=stored.artifact_id,
                user_scope_id=stored.user_scope_id,
                profile_fingerprint=stored.profile_fingerprint,
                candidate_snapshot_id=stored.candidate_snapshot_id,
                radio_snapshot_id=stored.radio_snapshot_id,
                created_at=stored.created_at,
                expires_at=stored.expires_at,
                payload={"rows": [{"kind": "changed"}]},
            )
        )


def test_account_reset_deletes_only_the_requested_home_scope(
    tmp_path: pathlib.Path,
) -> None:
    factory = _factory(tmp_path / "home.sqlite")
    repositories = HomeRepositorySet(factory)
    _seed_artifacts(repositories)
    state = repositories.state.get_or_create("user-1", now=100)
    repositories.state.initialize_active(
        "user-1", "feed-a", expected_revision=state.revision, now=101
    )
    repositories.jobs.enqueue(
        "user-1",
        HomeJobType.COMPOSE_FEED,
        "reset-test",
        now=102,
    )
    repositories.state.get_or_create("user-2", now=100)

    deleted = repositories.delete_scope("user-1")

    assert deleted["home_feed_state"] == 1
    assert deleted["home_feed_artifacts"] == 4
    assert deleted["home_candidate_snapshots"] == 1
    assert repositories.state.get("user-1") is None
    assert repositories.state.get("user-2") is not None


def test_jobs_are_idempotent_leased_retryable_and_terminal(tmp_path: pathlib.Path) -> None:
    jobs = HomeRepositorySet(_factory(tmp_path / "home.sqlite")).jobs
    queued = jobs.enqueue(
        "user-1",
        HomeJobType.REFRESH_CANDIDATES,
        "candidate-generation-1",
        payload={"reason": "ttl_expired"},
        max_attempts=2,
        now=100,
    )
    duplicate = jobs.enqueue(
        "user-1",
        HomeJobType.REFRESH_CANDIDATES,
        "candidate-generation-1",
        payload={"reason": "ttl_expired"},
        max_attempts=2,
        now=101,
    )
    assert duplicate.job_id == queued.job_id

    claimed = jobs.claim_next("worker-a", now=102, lease_seconds=10)
    assert claimed is not None
    assert claimed.status is HomeJobStatus.RUNNING
    assert claimed.attempts == 1
    assert claimed.lease_owner == "worker-a"

    retried = jobs.retry(claimed.job_id, claimed.lease_token, "provider timeout", now=103)
    assert retried.status is HomeJobStatus.RETRY
    assert retried.run_after == 108
    assert jobs.claim_next("worker-b", now=107) is None

    second = jobs.claim_next("worker-b", now=108, lease_seconds=10)
    assert second is not None
    terminal = jobs.retry(second.job_id, second.lease_token, "still unavailable", now=109)
    assert terminal.status is HomeJobStatus.FAILED
    assert jobs.claim_next("worker-c", now=200) is None

    with pytest.raises(LeaseLost):
        jobs.complete(second.job_id, second.lease_token, now=110)


def test_delayed_retry_is_durable_but_not_reported_as_building(
    tmp_path: pathlib.Path,
) -> None:
    jobs = HomeRepositorySet(_factory(tmp_path / "delayed-retry.sqlite")).jobs
    queued = jobs.enqueue(
        "user-1",
        HomeJobType.REFRESH_CANDIDATES,
        "candidate-refresh",
        max_attempts=3,
        now=100,
    )
    claimed = jobs.claim_next("worker-a", now=101, lease_seconds=10)
    assert claimed is not None and claimed.job_id == queued.job_id
    retried = jobs.retry(
        claimed.job_id,
        claimed.lease_token,
        "candidate_refresh_no_progress",
        delay_seconds=300,
        now=102,
    )

    assert retried.status is HomeJobStatus.RETRY
    assert jobs.has_active("user-1", now=200) is True
    assert jobs.has_runnable("user-1", now=200) is False
    assert jobs.has_runnable("user-1", now=402) is True


def test_artwork_jobs_coalesce_when_mutable_provider_payload_drifts(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "artwork-coalesce.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    scheduler = HomeJobScheduler(object())

    scheduled = [
        scheduler.artwork(
            {"user_scope_id": "user-1"},
            {
                "canonical_artist_id": "musicbrainz:artist:artist-1",
                "name": f"Mutable name {index}",
                "artwork_source_url": f"https://example.test/{index}.jpg",
            },
            profile_fingerprint="profile-1",
        )
        for index in range(40)
    ]

    assert len({job.job_id for job in scheduled}) == 1
    jobs = repositories.jobs.list_for_scope("user-1")
    assert len(jobs) == 1
    assert jobs[0].job_type is HomeJobType.REPAIR_ARTWORK


def test_startup_reconciles_polluted_artwork_queue_before_composition(
    tmp_path: pathlib.Path,
) -> None:
    factory = _factory(tmp_path / "polluted-queue.sqlite")
    repositories = HomeRepositorySet(factory)
    for index in range(140):
        identity = f"musicbrainz:artist:{index % 5}"
        repositories.jobs.enqueue(
            "user-1",
            HomeJobType.REPAIR_ARTWORK,
            f"polluted-artwork:{index}",
            payload={
                "request": {"user_scope_id": "user-1"},
                "artist": {
                    "canonical_artist_id": identity,
                    "artwork_source_url": f"https://example.test/{index}.jpg",
                },
            },
            priority=70,
            now=100 + index,
        )
    for index in range(2):
        repositories.jobs.enqueue(
            "user-1",
            HomeJobType.COMPOSE_FEED,
            f"compose:slot:user-1:{index + 1}",
            payload={"request": {"user_scope_id": "user-1"}},
            priority=40,
            now=300 + index,
        )

    restarted = HomeRepositorySet(factory)
    jobs = restarted.jobs.list_for_scope("user-1")
    actionable_artwork = [
        job
        for job in jobs
        if job.job_type is HomeJobType.REPAIR_ARTWORK
        and job.status in {HomeJobStatus.PENDING, HomeJobStatus.RETRY, HomeJobStatus.RUNNING}
    ]
    cancelled_artwork = [
        job
        for job in jobs
        if job.job_type is HomeJobType.REPAIR_ARTWORK
        and job.status is HomeJobStatus.CANCELLED
    ]
    claimed = restarted.jobs.claim_next("publication-worker", now=1_000)

    assert len(actionable_artwork) == 5
    assert len(cancelled_artwork) == 135
    assert claimed is not None
    assert claimed.job_type is HomeJobType.COMPOSE_FEED
    assert claimed.priority == 100


def test_expired_running_job_is_reclaimed_after_restart(tmp_path: pathlib.Path) -> None:
    factory = _factory(tmp_path / "home.sqlite")
    first_process = HomeRepositorySet(factory).jobs
    queued = first_process.enqueue(
        "user-1",
        HomeJobType.COMPOSE_FEED,
        "compose-1",
        now=100,
    )
    first_claim = first_process.claim_next("worker-before-restart", now=101, lease_seconds=5)
    assert first_claim is not None and first_claim.job_id == queued.job_id

    restarted = HomeRepositorySet(factory).jobs
    assert restarted.claim_next("worker-after-restart", now=105) is None
    reclaimed = restarted.claim_next("worker-after-restart", now=106)
    assert reclaimed is not None
    assert reclaimed.job_id == queued.job_id
    assert reclaimed.attempts == 2
    assert reclaimed.lease_token != first_claim.lease_token
    completed = restarted.complete(reclaimed.job_id, reclaimed.lease_token, now=107)
    assert completed.status is HomeJobStatus.SUCCEEDED


def test_expired_worker_lease_is_reconciled_before_status_is_reported(
    tmp_path: pathlib.Path,
) -> None:
    jobs = HomeRepositorySet(_factory(tmp_path / "expired.sqlite")).jobs
    queued = jobs.enqueue(
        "user-1", HomeJobType.REFRESH_CANDIDATES, "candidate-1", now=100
    )
    claimed = jobs.claim_next("dead-worker", now=101, lease_seconds=5)
    assert claimed is not None

    assert jobs.has_active("user-1", now=106) is True
    reconciled = jobs.get(queued.job_id)
    assert reconciled is not None
    assert reconciled.status is HomeJobStatus.RETRY
    assert reconciled.last_error == "lease_expired_worker_lost"
    replacement = jobs.claim_next("replacement-worker", now=106)
    assert replacement is not None
    assert replacement.attempts == 2
    assert replacement.lease_owner == "replacement-worker"


def test_job_leases_serialize_each_user_lane_without_blocking_other_lanes_or_users(
    tmp_path: pathlib.Path,
) -> None:
    jobs = HomeRepositorySet(_factory(tmp_path / "home.sqlite")).jobs
    first = jobs.enqueue(
        "user-1", HomeJobType.REFRESH_CANDIDATES, "candidate-1", now=100
    )
    same_user = jobs.enqueue(
        "user-1", HomeJobType.REFRESH_RADIO, "radio-1", now=101
    )
    other_user = jobs.enqueue(
        "user-2", HomeJobType.REFRESH_CANDIDATES, "candidate-2", now=102
    )

    claimed_first = jobs.claim_next("worker-a", now=103, lease_seconds=30)
    claimed_other_lane = jobs.claim_next("worker-b", now=103, lease_seconds=30)
    claimed_other_user = jobs.claim_next("worker-c", now=103, lease_seconds=30)

    assert claimed_first is not None and claimed_first.job_id == first.job_id
    assert claimed_other_lane is not None and claimed_other_lane.job_id == same_user.job_id
    assert claimed_other_user is not None and claimed_other_user.job_id == other_user.job_id
    assert jobs.claim_next("worker-d", now=103, lease_seconds=30) is None

    jobs.complete(claimed_first.job_id, claimed_first.lease_token, now=104)


def test_repository_set_uses_the_existing_recommendation_store_adapter(
    tmp_path: pathlib.Path,
) -> None:
    server = SimpleNamespace(
        RECOMMENDATION_STORE_DB_PATH=str(tmp_path / "recommendation.sqlite3"),
        recommendation_store_lock=threading.RLock(),
    )
    repositories = HomeRepositorySet.from_server(server)
    assert repositories.state.get_or_create("user-1", now=100).user_scope_id == "user-1"


def test_exposure_ledger_persists_cooldowns_and_excludes_history_rows(
    tmp_path: pathlib.Path,
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "exposure.sqlite"))
    repositories.exposures.record(
        "user-1",
        {
            ("todays_pick", "track", "Track-New"),
            ("recommended_artists", "artist", "Artist-New"),
            ("popular_radio", "artist", "Artist-Radio"),
            ("last_played", "track", "Track-History"),
        },
        now=100,
    )

    assert repositories.exposures.recent_identities(
        "user-1", since=90
    ) == {"track-new", "artist-new", "artist-radio"}
    assert repositories.exposures.recent_radio_artist_identities(
        "user-1", since=90
    ) == {"artist-radio"}
    assert repositories.exposures.recent_identities("user-1", since=101) == set()
