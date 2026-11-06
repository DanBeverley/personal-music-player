from __future__ import annotations

from dataclasses import replace
import pathlib
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace


CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.discovery.enrichment import (  # noqa: E402
    CandidateEnrichmentPlan,
    MaterializedCandidateSupply,
    build_enrichment_plan,
)
from auralis_backend.discovery.inventory import (  # noqa: E402
    CandidateInventory,
    candidate_inventory_from_payload,
    candidate_inventory_supply_status,
)
from auralis_backend.discovery.radio_inventory import (  # noqa: E402
    ArtistRadioInventory,
    RADIO_DISCOVERY_RESERVE_TARGET_CARDS,
    RADIO_WORKING_RESERVOIR_TARGET_CARDS,
)
from auralis_backend.discovery.schema import DiscoveryCandidate, TasteProfile  # noqa: E402
from auralis_backend.home_feed.candidate_supply import (  # noqa: E402
    GENERAL_CANDIDATE_POOLS,
    CandidateSupplyOutcome,
    CandidateSupplyService,
    exposure_aware_candidate_supply_status,
)
from auralis_backend.home_feed.contracts import (  # noqa: E402
    CandidateSnapshotRecord,
    HomeJobStatus,
    HomeJobType,
    home_supply_profile_fingerprint,
)
from auralis_backend.home_feed.composer import ComposeOutcome  # noqa: E402
from auralis_backend.home_feed.radio_supply import (  # noqa: E402
    ArtworkSupplyService,
    RadioSupplyOutcome,
    RadioSupplyService,
    _radio_expansion_needed,
)
from auralis_backend.home_feed.repositories import HomeRepositorySet  # noqa: E402
from auralis_backend.home_feed.scheduler import HomeJobScheduler  # noqa: E402
import auralis_backend.home_feed.worker as home_worker  # noqa: E402
from auralis_backend.home_feed.worker import (  # noqa: E402
    HomeFeedWorker,
    HomeWorkerResult,
)


def _factory(path: pathlib.Path):
    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    return connect


def _taste(profile_key: str = "profile-1") -> TasteProfile:
    return TasteProfile(
        user_scope_id="user-1",
        profile_key=profile_key,
        signal_tier="established",
    )


def test_owned_home_lanes_execute_without_provider_head_of_line_blocking(
    monkeypatch,
) -> None:
    barrier = threading.Barrier(4)

    class FakeWorker:
        def __init__(self, _server, *, worker_id="", job_types=()):
            self.job_type = job_types[0]

        def run_once(self):
            barrier.wait(timeout=2)
            return HomeWorkerResult(
                claimed=True,
                job_id=self.job_type.value,
                job_type=self.job_type.value,
                outcome="succeeded",
            )

    monkeypatch.setattr(home_worker, "HomeFeedWorker", FakeWorker)

    results = home_worker.run_home_feed_lanes(object(), max_jobs=4)

    assert {result.job_type for result in results} == {
        "compose_feed",
        "refresh_candidates",
        "refresh_radio",
        "repair_artwork",
    }


def _seed_candidate(
    repositories: HomeRepositorySet,
    snapshot_id: str,
    *,
    generated_at: float | None = None,
) -> None:
    now = time.time() if generated_at is None else generated_at
    repositories.candidates.put(
        CandidateSnapshotRecord(
            snapshot_id=snapshot_id,
            user_scope_id="user-1",
            profile_fingerprint=home_supply_profile_fingerprint("user-1"),
            generated_at=now,
            expires_at=now + 3600,
            payload={"generation_id": snapshot_id},
        )
    )


def test_album_reserve_counts_unseen_artists_not_raw_album_cards(tmp_path) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "album-reserve.sqlite"))
    albums = [
        DiscoveryCandidate(
            item={
                "id": f"album-{index}",
                "title": f"Album {index}",
                "artist": f"Artist {index}",
                "playable": True,
                "playable_source_id": f"track-{index}",
            },
            source="album",
            item_type="album",
        )
        for index in range(20)
    ]
    inventory = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint=home_supply_profile_fingerprint("user-1"),
        generated_at=time.time(),
        expires_at=time.time() + 3600,
        generation_id="candidate-albums",
        pools={"album": albums},
        coverage={"ready": True},
    )
    repositories.exposures.record(
        "user-1",
        [
            ("featured_new_albums", "artist", f"artist:artist {index}")
            for index in range(12)
        ],
    )

    status = exposure_aware_candidate_supply_status(
        inventory,
        repositories=repositories,
        user_scope_id="user-1",
    )

    assert status["album_unexposed_artist_count"] == 8
    assert status["album_unexposed_artist_deficit"] == 8
    assert status["expansion_needed"] is True


def test_unseen_album_shortage_prioritizes_discovery_artist_acquisition() -> None:
    plan = build_enrichment_plan(
        _taste(),
        acquisition_ledger={
            "album_unexposed_artist_deficit": 6,
            "qualified_album_reserve_shortage": 6,
            "album_priority_artist_seeds": ["New Artist", "Second Artist"],
            "album_shelf_shortages": {"featured_new_albums": 6},
        },
        allowed_pools=GENERAL_CANDIDATE_POOLS,
    )
    album_requests = [request for request in plan.requests if request.pool == "album"]

    assert plan.priority_pools[0] == "album"
    assert album_requests
    assert album_requests[0].key == "New Artist"
    assert album_requests[0].provider == "ytmusic"


def test_candidate_supply_excludes_radio_provider_work(monkeypatch, tmp_path) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "candidate.sqlite"))
    now = time.time()
    existing = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint=home_supply_profile_fingerprint("user-1"),
        generated_at=now,
        expires_at=now + 3600,
        generation_id="candidate-before",
        coverage={"ready": False},
    )
    observed: dict[str, object] = {}

    def plan(_taste, *, acquisition_ledger, allowed_pools):
        observed["allowed_pools"] = allowed_pools
        return CandidateEnrichmentPlan(user_scope_id="user-1")

    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.load_candidate_inventory",
        lambda *_args, **_kwargs: existing,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.build_enrichment_plan", plan
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.materialize_enrichment_plan",
        lambda *_args, **_kwargs: MaterializedCandidateSupply(
            diagnostics={"scheduled_request_count": 0}
        ),
    )
    outcome = CandidateSupplyService(object(), repositories).refresh(
        _taste(),
        force=True,
        max_cycles=1,
    )

    assert observed["allowed_pools"] == GENERAL_CANDIDATE_POOLS
    assert "radio_artist_catalog" not in observed["allowed_pools"]
    assert outcome.reason == "candidate_no_progress"
    assert outcome.retryable is False
    assert outcome.retry_after_seconds == 300


def test_identical_usable_supply_keeps_one_revision_and_enters_backoff(
    monkeypatch, tmp_path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "same-supply.sqlite"))
    now = time.time()
    track = DiscoveryCandidate(
        item={
            "id": "track-1",
            "title": "Track One",
            "artist": "Artist One",
            "playable": True,
            "playable_source_id": "track-1",
            "provider": "ytmusic",
        },
        source="similarity",
        item_type="track",
    )
    existing = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint=home_supply_profile_fingerprint("user-1"),
        generated_at=now,
        expires_at=now + 3600,
        generation_id="candidate-stable",
        pools={"similarity": [track]},
        coverage={"ready": True, "actual": {"unplayed_tracks": 1}},
    )
    rebuilt = CandidateInventory(
        **{
            **existing.__dict__,
            "generation_id": "candidate-false-new-revision",
            "base_generation_id": existing.generation_id,
        }
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.load_candidate_inventory",
        lambda *_args, **_kwargs: existing,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.build_enrichment_plan",
        lambda *_args, **_kwargs: CandidateEnrichmentPlan(user_scope_id="user-1"),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.materialize_enrichment_plan",
        lambda *_args, **_kwargs: MaterializedCandidateSupply(
            pools={"similarity": [dict(track.item)]},
            diagnostics={"scheduled_request_count": 1},
        ),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.verify_materialized_supply",
        lambda *_args, **_kwargs: MaterializedCandidateSupply(
            pools={"similarity": [dict(track.item)]},
            diagnostics={"scheduled_request_count": 1},
        ),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.build_candidate_inventory",
        lambda *_args, **_kwargs: rebuilt,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.complete_inventory_release_metadata",
        lambda _server, inventory, **_kwargs: inventory,
    )

    outcome = CandidateSupplyService(object(), repositories).refresh(
        _taste(),
        force=True,
    )

    assert outcome.snapshot_id == "candidate-stable"
    assert outcome.made_progress is False
    assert outcome.retry_after_seconds == 300
    assert repositories.candidates.get("candidate-false-new-revision") is None


def test_candidate_watermarks_report_low_and_target_reserves_separately() -> None:
    inventory = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint=home_supply_profile_fingerprint("user-1"),
        generated_at=time.time(),
        expires_at=time.time() + 3600,
        coverage={
            "ready": True,
            "actual": {
                "unplayed_tracks": 180,
                "qualified_album_inventory": 30,
                "recommended_artists": 45,
                "quiet_picks": 180,
            },
        },
    )

    status = candidate_inventory_supply_status(inventory)

    assert status["below_low_watermark"] is False
    assert status["expansion_needed"] is True


def test_expired_candidate_inventory_enters_real_refresh_path_without_crashing(
    monkeypatch, tmp_path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "expired-candidate.sqlite"))
    now = time.time()
    expired = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=now - (9 * 60 * 60),
        expires_at=now - 1,
        generation_id="candidate-expired",
        pools={},
        coverage={"ready": True},
    )
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.load_candidate_inventory",
        lambda *_args, **_kwargs: expired,
    )

    def plan(_taste, *, acquisition_ledger, allowed_pools):
        observed["refresh_required"] = acquisition_ledger.get(
            "candidate_refresh_required"
        )
        observed["refresh_reason"] = acquisition_ledger.get(
            "candidate_refresh_reason"
        )
        return CandidateEnrichmentPlan(user_scope_id="user-1")

    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.build_enrichment_plan", plan
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.materialize_enrichment_plan",
        lambda *_args, **_kwargs: MaterializedCandidateSupply(
            diagnostics={"scheduled_request_count": 0}
        ),
    )

    outcome = CandidateSupplyService(object(), repositories).refresh(
        _taste(),
        force=True,
        max_cycles=1,
    )

    assert observed == {
        "refresh_required": True,
        "refresh_reason": "revalidation_due",
    }
    assert outcome.reason == "candidate_snapshot_reused_no_progress"
    assert outcome.ready is False
    assert outcome.retryable is False


def test_expired_ready_inventory_reaches_provider_and_publishes_returned_supply(
    monkeypatch, tmp_path
) -> None:
    """Regression for stale counts suppressing the actual refresh request."""

    repositories = HomeRepositorySet(_factory(tmp_path / "expired-refresh-result.sqlite"))
    now = time.time()
    stable_profile = home_supply_profile_fingerprint("user-1")
    old_track = DiscoveryCandidate(
        item={
            "id": "old-track",
            "title": "Old Track",
            "artist": "Seed Artist",
            "provider": "ytmusic",
            "playable": True,
            "playable_source_id": "old-track",
            "source_authority": "canonical",
        },
        source="similarity",
        item_type="track",
    )
    expired = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint=stable_profile,
        generated_at=now - (9 * 60 * 60),
        expires_at=now - 1,
        generation_id="candidate-expired-ready",
        pools={"similarity": [old_track]},
        coverage={"ready": True},
        acquisition_ledger={
            "failed_domains": [],
            "optional_row_counts": {
                "featured_new_albums": 10,
                "popular_radio": 12,
                "recommended_albums": 12,
            },
            "row_reserve_shortages": {
                "because_you_played": 0,
                "quiet_picks": 0,
                "made_for_you_tracks": 0,
                "recommended_artists": 0,
            },
        },
    )
    taste = TasteProfile(
        user_scope_id="user-1",
        profile_key=stable_profile,
        signal_tier="established",
        recent_tracks=[dict(old_track.item)],
        artist_hints=["Seed Artist"],
    )
    provider_calls: list[tuple[str, str]] = []
    stored_progress: list[dict[str, object]] = []

    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.load_candidate_inventory",
        lambda *_args, **_kwargs: expired,
    )
    # Simulate the old 14-day provider cache. A new candidate refresh
    # generation must bypass it exactly once instead of accepting it as fresh.
    monkeypatch.setattr(
        "auralis_backend.discovery.enrichment._load_job",
        lambda *_args, **_kwargs: {
            "state": "resolved",
            "expires_at": now + (5 * 24 * 60 * 60),
            "results": [dict(old_track.item)],
        },
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.enrichment._store_job",
        lambda _server, _scope, _request, progress: stored_progress.append(
            dict(progress)
        ),
    )

    def fetch(_server, request):
        provider_calls.append((request.kind, request.pool))
        identity = f"new-{request.pool}-{len(provider_calls)}"
        return [
            {
                "id": identity,
                "title": f"New Track {len(provider_calls)}",
                "artist": f"Discovery Artist {len(provider_calls)}",
                "provider": "ytmusic",
                "playable": True,
                "playable_source_id": identity,
                "source_authority": "canonical",
                "materialized_relation": request.relation,
                "recommendation_path": request.relation,
            }
        ]

    monkeypatch.setattr(
        "auralis_backend.discovery.enrichment._fetch_request", fetch
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.verify_materialized_supply",
        lambda _server, supply, _taste, **_kwargs: supply,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.store_candidate_inventory",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.complete_inventory_release_metadata",
        lambda _server, inventory, **_kwargs: inventory,
    )

    def ready_coverage(inventory, **_kwargs):
        return replace(
            inventory,
            coverage={"ready": True, "actual": {"unique_tracks": 200}},
        )

    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.refresh_candidate_inventory_coverage",
        ready_coverage,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.inventory.refresh_candidate_inventory_coverage",
        ready_coverage,
    )

    outcome = CandidateSupplyService(object(), repositories).refresh(
        taste,
        force=True,
        max_cycles=1,
    )

    assert provider_calls
    assert any(pool != "album" for _kind, pool in provider_calls)
    assert outcome.scheduled_requests > 0
    assert outcome.made_progress is True
    assert outcome.ready is True
    assert outcome.reason == "candidate_snapshot_ready"
    assert outcome.snapshot_id not in {"", expired.generation_id}
    published = repositories.candidates.get(outcome.snapshot_id)
    assert published is not None
    published_inventory = candidate_inventory_from_payload(dict(published.payload))
    assert published_inventory is not None
    assert any(
        str(candidate.item.get("id") or "").startswith("new-")
        for candidates in published_inventory.pools.values()
        for candidate in candidates
    )
    assert any(progress.get("refresh_generation") for progress in stored_progress)


def test_candidate_supply_consolidates_an_old_profile_without_losing_facts(
    monkeypatch, tmp_path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "profile-drift.sqlite"))
    now = time.time()
    wrong_profile = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-old",
        generated_at=now,
        expires_at=now + 3600,
        generation_id="candidate-old",
        coverage={"ready": True},
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.load_candidate_inventory",
        lambda *_args, **_kwargs: wrong_profile,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.refresh_candidate_inventory_coverage",
        lambda inventory, **_kwargs: inventory,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.candidate_supply.build_enrichment_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("profile cutover must not wait for provider acquisition")
        ),
    )

    outcome = CandidateSupplyService(object(), repositories).refresh(
        _taste("profile-current"),
        force=True,
    )

    assert outcome.ready is True
    assert outcome.made_progress is True
    assert outcome.scheduled_requests == 0
    assert outcome.reason == "consolidated_inventory_ready"
    migrated = repositories.candidates.get(outcome.snapshot_id)
    assert migrated is not None
    assert migrated.profile_fingerprint == home_supply_profile_fingerprint("user-1")
    assert migrated.snapshot_id != "candidate-old"


def test_candidate_scheduler_supersedes_stale_pending_generation(
    monkeypatch, tmp_path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "candidate-singleton.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    scheduler = HomeJobScheduler(object())

    first = scheduler.candidates(
        {"user_scope_id": "user-1"},
        reason="candidate_inventory_refresh",
        generation_hint="launch",
    )
    second = scheduler.candidates(
        {"user_scope_id": "user-1"},
        reason="inventory_replenish",
        generation_hint="compose-missing",
    )

    assert second.job_id != first.job_id
    assert repositories.jobs.get(first.job_id).status is HomeJobStatus.CANCELLED
    assert sum(
        job.job_type is HomeJobType.REFRESH_CANDIDATES
        and job.status in {
            HomeJobStatus.PENDING,
            HomeJobStatus.RETRY,
            HomeJobStatus.RUNNING,
        }
        for job in repositories.jobs.list_for_scope("user-1")
    ) == 1


def test_compose_scheduler_coalesces_the_exact_supply_pair(
    monkeypatch, tmp_path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "compose-pair.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    scheduler = HomeJobScheduler(object())

    first = scheduler.compose(
        {"user_scope_id": "user-1"},
        reason="candidate_snapshot_ready",
        candidate_snapshot_id="candidate-1",
        radio_snapshot_id="radio-1",
        generation_hint="caller-a",
    )
    second = scheduler.compose(
        {"user_scope_id": "user-1"},
        reason="radio_snapshot_ready",
        candidate_snapshot_id="candidate-1",
        radio_snapshot_id="radio-1",
        generation_hint="caller-b",
    )

    assert second.job_id == first.job_id
    assert sum(
        job.job_type is HomeJobType.COMPOSE_FEED
        for job in repositories.jobs.list_for_scope("user-1")
    ) == 1


def test_radio_scheduler_replaces_stale_pending_candidate_generation(
    monkeypatch, tmp_path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "radio-singleton.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    scheduler = HomeJobScheduler(object())

    old = scheduler.radio(
        {"user_scope_id": "user-1"},
        reason="old-supply",
        candidate_snapshot_id="candidate-old",
        generation_hint="candidate-old",
    )
    current = scheduler.radio(
        {"user_scope_id": "user-1"},
        reason="current-supply",
        candidate_snapshot_id="candidate-current",
        generation_hint="candidate-current",
    )

    assert current.job_id != old.job_id
    assert repositories.jobs.get(old.job_id).status is HomeJobStatus.CANCELLED
    assert current.payload["candidate_snapshot_id"] == "candidate-current"


def test_radio_supply_owns_only_radio_acquisition_and_stops_when_nothing_schedules(
    monkeypatch, tmp_path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "radio.sqlite"))
    now = time.time()
    candidate = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=now,
        expires_at=now + 3600,
        generation_id="candidate-ready",
        coverage={"ready": True},
    )
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.load_candidate_inventory",
        lambda *_args, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.load_artist_radio_inventory",
        lambda *_args, **_kwargs: None,
    )

    def plan(_taste, **kwargs):
        observed.update(kwargs)
        return CandidateEnrichmentPlan(user_scope_id="user-1")

    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.build_enrichment_plan", plan
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.materialize_enrichment_plan",
        lambda *_args, **_kwargs: MaterializedCandidateSupply(
            diagnostics={"scheduled_request_count": 0}
        ),
    )

    outcome = RadioSupplyService(object(), repositories).refresh(
        SimpleNamespace(
            user_scope_id="user-1",
            profile_key="profile-1",
            listened_artists=[],
            top_artists=[],
            artist_hints=[],
        )
    )

    assert observed["allowed_pools"] == {"radio_artist_catalog"}
    assert outcome.reason == "no_scheduled_radio_work"
    assert outcome.retryable is True


def test_radio_discovery_seeds_skip_familiar_occupied_and_saturated_artists(
    tmp_path,
) -> None:
    service = RadioSupplyService(
        object(), HomeRepositorySet(_factory(tmp_path / "seeds.sqlite"))
    )
    inventory = SimpleNamespace(
        pools={
            "artist_graph": [
                SimpleNamespace(item={"artist": "Familiar", "provider_artist_id": "a"}),
                SimpleNamespace(item={"artist": "Saturated", "provider_artist_id": "b"}),
                SimpleNamespace(item={"artist": "Fresh", "provider_artist_id": "c"}),
            ],
            "popularity": [
                SimpleNamespace(item={"artist": "Occupied", "provider_artist_id": "d"})
            ],
        }
    )
    seeds = service._discovery_seeds(
        inventory,
        SimpleNamespace(
            listened_artists=["Familiar"], top_artists=[], artist_hints=[]
        ),
        [{"seed_artist_identity": "provider:artist:d"}],
        own_counts={"provider:artist:b": 12},
    )

    assert [seed["name"] for seed in seeds] == ["Fresh"]


def test_radio_discovery_seeds_accept_similarity_canonical_artist_identity(
    tmp_path,
) -> None:
    service = RadioSupplyService(
        object(), HomeRepositorySet(_factory(tmp_path / "similarity-seeds.sqlite"))
    )
    inventory = SimpleNamespace(
        pools={
            "similarity": [
                SimpleNamespace(
                    item={
                        "artist": "Adjacent Artist",
                        "canonical_artist_id": "musicbrainz:artist:adjacent-1",
                    }
                )
            ]
        }
    )

    seeds = service._discovery_seeds(
        inventory,
        SimpleNamespace(listened_artists=[], top_artists=[], artist_hints=[]),
        [],
        own_counts={},
    )

    assert seeds == [
        {
            "key": "musicbrainz:artist:adjacent-1",
            "name": "Adjacent Artist",
            "musicbrainz_artist_id": "adjacent-1",
            "provider_artist_id": None,
            "source": "similarity",
        }
    ]


def test_radio_replenishment_is_demand_based_not_fixed_to_full_storage() -> None:
    cards = [
        {
            "id": f"radio-{index}",
            "seed_artist_identity": f"artist-{index}",
            "seed_affinity": (
                "exploratory"
                if index < RADIO_DISCOVERY_RESERVE_TARGET_CARDS
                else "direct"
            ),
        }
        for index in range(RADIO_WORKING_RESERVOIR_TARGET_CARDS)
    ]
    inventory = ArtistRadioInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=time.time(),
        expires_at=time.time() + 3600,
        generation_id="radio-demand-ready",
        cards=cards[:10],
        reservoir_cards=cards,
        diagnostics={
            "discovery_card_count": RADIO_DISCOVERY_RESERVE_TARGET_CARDS,
            "unexposed_deficit": 0,
            "visible_discovery_target_deficit": 0,
        },
    )

    assert _radio_expansion_needed(inventory) is False
    inventory.diagnostics["unexposed_deficit"] = 4
    assert _radio_expansion_needed(inventory) is True


def test_radio_snapshot_revision_is_idempotent_for_same_verified_supply(
    tmp_path,
) -> None:
    database_path = tmp_path / "semantic-radio.sqlite"
    repositories = HomeRepositorySet(_factory(database_path))
    now = time.time()
    card = {
        "id": "radio-artist-1",
        "seed_artist_identity": "musicbrainz:artist:artist-1",
        "artwork_owner_identity": "musicbrainz:artist:artist-1",
        "thumbnail": "/artist_artwork/artist-1",
        "tracks": [{"id": f"track-{index}"} for index in range(12)],
    }
    first = ArtistRadioInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=now,
        expires_at=now + 3600,
        generation_id="attempt-1",
        cards=[card],
        reservoir_cards=[card],
        diagnostics={"request_progress": 1},
    )
    second = ArtistRadioInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=now + 1,
        expires_at=now + 3601,
        generation_id="attempt-2",
        cards=[dict(card)],
        reservoir_cards=[dict(card)],
        diagnostics={"request_progress": 99},
    )
    service = RadioSupplyService(object(), repositories)

    first_id = service._publish_snapshot(first)
    second_id = service._publish_snapshot(second)

    assert second_id == first_id
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM home_radio_snapshots"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_candidate_snapshot_ignores_mutable_ledger_and_detached_radio_pools(
    tmp_path,
) -> None:
    database_path = tmp_path / "candidate-ledger-idempotency.sqlite"
    repositories = HomeRepositorySet(_factory(database_path))
    now = time.time()
    candidate = DiscoveryCandidate(
        item_type="track",
        item={"id": "track-1", "title": "Track One", "artist": "Artist One"},
        source="similarity",
        score=0.9,
    )
    legacy_radio_candidate = DiscoveryCandidate(
        item_type="track",
        item={
            "id": "radio-track-1",
            "title": "Radio Track",
            "artist": "Artist Two",
        },
        source="radio_artist_catalog",
        score=0.8,
    )
    first = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint=home_supply_profile_fingerprint("user-1"),
        generated_at=now,
        expires_at=now + 3600,
        generation_id="candidate-generation-1",
        pools={
            "similarity": [candidate],
            "radio_artist_catalog": [legacy_radio_candidate],
        },
        coverage={"ready": True},
        acquisition_ledger={"consecutive_no_progress_batches": 0},
    )
    service = CandidateSupplyService(object(), repositories)

    first_id = service._publish_snapshot(first)
    second_id = service._publish_snapshot(
        replace(
            first,
            pools={"similarity": [candidate]},
            acquisition_ledger={"consecutive_no_progress_batches": 1},
        )
    )

    assert second_id == first_id
    stored = repositories.candidates.get(first_id)
    assert stored is not None
    assert stored.payload["acquisition_ledger"][
        "consecutive_no_progress_batches"
    ] == 0
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM home_candidate_snapshots"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_artwork_supply_persists_verified_artwork_synchronously(monkeypatch) -> None:
    remembered: list[dict[str, object]] = []
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.cache_artist_artwork_now",
        lambda _server, record: {
            **record,
            "thumbnail": "/artist_artwork/artist-1.jpg",
        },
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.remember_catalog_entity",
        lambda _server, **kwargs: remembered.append(kwargs),
    )

    assert ArtworkSupplyService(object()).repair(
        {"canonical_artist_id": "artist-1", "name": "Artist One"}
    )
    assert remembered[0]["source"] == "home_radio_artwork_worker"


def test_artwork_supply_resolves_exact_artist_entity_before_r2_cache(
    monkeypatch,
) -> None:
    captured: list[dict[str, object]] = []

    class _YtMusic:
        def search(self, query, *, filter, limit):
            assert query == "New Artist"
            assert filter == "artists"
            return [
                {
                    "artist": "New Artist",
                    "browseId": "UC-new-artist",
                    "thumbnails": [{"url": "https://img.test/search.jpg"}],
                }
            ]

        def get_artist(self, artist_id):
            assert artist_id == "UC-new-artist"
            return {"thumbnails": [{"url": "https://img.test/profile.jpg"}]}

    def cache(_server, record):
        captured.append(dict(record))
        return {**record, "thumbnail": "/artist_artwork/verified-token"}

    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.cache_artist_artwork_now",
        cache,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.remember_catalog_entity",
        lambda *_args, **_kwargs: None,
    )
    server = SimpleNamespace(ytmusic=_YtMusic())

    assert ArtworkSupplyService(server).repair(
        {
            "canonical_artist_id": "musicbrainz:artist:new-artist",
            "name": "New Artist",
        }
    )
    assert captured[0]["canonical_artist_id"] == "musicbrainz:artist:new-artist"
    assert captured[0]["provider_artist_id"] == "UC-new-artist"
    assert "https://img.test/profile.jpg" in captured[0]["artwork_source_urls"]


class _CandidateSupply:
    def refresh(self, _taste, *, force=False):
        return CandidateSupplyOutcome(
            snapshot_id="candidate-ready",
            ready=True,
            made_progress=True,
            candidate_count=480,
            reason="candidate_snapshot_ready",
        )


class _RadioSupply:
    def refresh(self, _taste, *, candidate_snapshot_id=""):
        return RadioSupplyOutcome(
            snapshot_id="radio-ready",
            ready=True,
            made_progress=True,
            reservoir_count=36,
            discovery_count=12,
            artwork_records=(
                {
                    "canonical_artist_id": "artist-1",
                    "name": "Artist One",
                    "artwork_source_url": "https://example.test/artist.jpg",
                },
            ),
            reason="radio_snapshot_ready",
        )

    def recompose(self, _taste):
        return RadioSupplyOutcome(
            snapshot_id="radio-after-artwork",
            ready=True,
            reservoir_count=36,
            discovery_count=12,
            reason="radio_recomposed",
        )


class _ArtworkSupply:
    def repair(self, _record):
        return True


def test_successful_artwork_batch_schedules_only_one_radio_reconcile(
    monkeypatch, tmp_path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "artwork-radio-batch.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.worker.build_taste_profile",
        lambda _server, req: SimpleNamespace(
            user_scope_id=req.user_scope_id,
            profile_key="profile-1",
        ),
    )
    _seed_candidate(repositories, "candidate-ready")
    scheduler = HomeJobScheduler(object())
    for index in range(2):
        scheduler.artwork(
            {"user_scope_id": "user-1"},
            {
                "canonical_artist_id": f"artist-{index}",
                "name": f"Artist {index}",
                "artwork_source_url": f"https://example.test/{index}.jpg",
            },
            profile_fingerprint="profile-1",
        )

    worker = HomeFeedWorker(object(), worker_id="artwork-batch-worker")
    worker._artwork_supply = _ArtworkSupply()
    worker._radio_supply = _RadioSupply()
    worker._handlers[HomeJobType.COMPOSE_FEED] = lambda _job: None
    worker.run_until_idle(max_jobs=8)

    jobs = repositories.jobs.list_for_scope("user-1")
    radio_jobs = [
        job for job in jobs if job.job_type is HomeJobType.REFRESH_RADIO
    ]
    assert len(radio_jobs) == 1
    assert radio_jobs[0].status is HomeJobStatus.SUCCEEDED


def test_ready_radio_publishes_then_bounded_job_continues_reserve_expansion(
    monkeypatch, tmp_path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "radio-expansion.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.worker.build_taste_profile",
        lambda _server, req: SimpleNamespace(
            user_scope_id=req.user_scope_id,
            profile_key="profile-1",
        ),
    )
    _seed_candidate(repositories, "candidate-ready")
    HomeJobScheduler(object()).radio(
        {"user_scope_id": "user-1"},
        reason="candidate_snapshot_ready",
        candidate_snapshot_id="candidate-ready",
        generation_hint="radio-expansion",
    )
    worker = HomeFeedWorker(object(), worker_id="radio-expansion-worker")
    worker._radio_supply = SimpleNamespace(
        refresh=lambda _taste, **_kwargs: RadioSupplyOutcome(
            snapshot_id="radio-ready-new",
            ready=True,
            made_progress=True,
            scheduled_requests=1,
            reservoir_count=24,
            discovery_count=5,
            expansion_needed=True,
            reason="radio_snapshot_ready",
        )
    )

    result = worker.run_once()
    jobs = repositories.jobs.list_for_scope("user-1")

    assert result.outcome == "retry"
    assert any(
        job.job_type is HomeJobType.COMPOSE_FEED
        and job.status is HomeJobStatus.PENDING
        for job in jobs
    )
    radio_job = next(job for job in jobs if job.job_type is HomeJobType.REFRESH_RADIO)
    assert radio_job.status is HomeJobStatus.RETRY
    assert radio_job.last_error == "radio_reserve_replenish"


def test_stored_feed_requests_next_allocation_from_same_verified_supply(
    monkeypatch, tmp_path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "successor-supply.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.worker.build_taste_profile",
        lambda _server, req: SimpleNamespace(
            user_scope_id=req.user_scope_id,
            profile_key="profile-1",
        ),
    )
    HomeJobScheduler(object()).compose(
        {"user_scope_id": "user-1"},
        reason="initial",
        candidate_snapshot_id="candidate-a",
        radio_snapshot_id="radio-a",
        generation_hint="initial-compose",
    )
    worker = HomeFeedWorker(object(), worker_id="successor-worker")
    worker._composer = SimpleNamespace(
        compose=lambda *_args, **_kwargs: ComposeOutcome(
            stored=True,
            artifact_id="feed-a",
            candidate_snapshot_id="candidate-a",
            radio_snapshot_id="radio-a",
            reason="ready_appended",
        )
    )
    monkeypatch.setattr(
        repositories.state,
        "reconcile",
        lambda _scope: SimpleNamespace(
            active_version=1,
            ready_artifact_ids=(),
        ),
    )

    result = worker.run_once()
    jobs = repositories.jobs.list_for_scope("user-1")

    assert result.outcome == "succeeded"
    assert any(
        job.job_type is HomeJobType.COMPOSE_FEED
        and job.status is HomeJobStatus.PENDING
        and job.payload["candidate_snapshot_id"] == "candidate-a"
        and job.payload["radio_snapshot_id"] == "radio-a"
        and job.payload["allocation_epoch"] == 2
        for job in jobs
    )
    assert not any(
        job.job_type is HomeJobType.REFRESH_CANDIDATES
        and job.status is HomeJobStatus.PENDING
        for job in jobs
    )


def test_compose_slot_consumes_latest_supply_available_when_worker_runs(
    monkeypatch, tmp_path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "latest-compose-supply.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.worker.build_taste_profile",
        lambda _server, req: SimpleNamespace(
            user_scope_id=req.user_scope_id,
            profile_key=home_supply_profile_fingerprint(req.user_scope_id),
        ),
    )
    now = time.time()
    _seed_candidate(repositories, "candidate-old", generated_at=now - 2)
    HomeJobScheduler(object()).compose(
        {"user_scope_id": "user-1"},
        reason="initial",
        candidate_snapshot_id="candidate-old",
        allocation_epoch=1,
    )
    _seed_candidate(repositories, "candidate-new", generated_at=now - 1)
    observed: dict[str, str] = {}

    def compose(_taste, **kwargs):
        observed["candidate_snapshot_id"] = kwargs["candidate_snapshot_id"]
        return ComposeOutcome(
            stored=False,
            candidate_snapshot_id=kwargs["candidate_snapshot_id"],
            radio_snapshot_id=kwargs["radio_snapshot_id"],
            reason="artifact_unchanged",
        )

    worker = HomeFeedWorker(object(), worker_id="latest-supply-worker")
    worker._composer = SimpleNamespace(compose=compose)

    result = worker.run_once()

    assert result.outcome == "succeeded"
    assert observed["candidate_snapshot_id"] == "candidate-new"


class _IncompleteFloodRadioSupply:
    def refresh(self, _taste, *, candidate_snapshot_id=""):
        return RadioSupplyOutcome(
            ready=False,
            reservoir_count=19,
            discovery_count=3,
            artwork_records=tuple(
                {
                    "canonical_artist_id": f"musicbrainz:artist:{index % 5}",
                    "name": f"Artist {index % 5}",
                    "artwork_source_url": f"https://example.test/{index}.jpg",
                }
                for index in range(140)
            ),
            retryable=True,
            reason="radio_contract_incomplete",
        )


def test_usage_cycle_is_owned_by_durable_jobs(monkeypatch, tmp_path) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "worker.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.worker.build_taste_profile",
        lambda _server, req: SimpleNamespace(
            user_scope_id=req.user_scope_id,
            profile_key="profile-1",
        ),
    )
    scheduler = HomeJobScheduler(object())
    scheduler.candidates(
        {"user_scope_id": "user-1"},
        reason="candidate_inventory_refresh",
        generation_hint="usage-cycle",
    )
    _seed_candidate(repositories, "candidate-ready")
    worker = HomeFeedWorker(object(), worker_id="test-worker")
    worker._candidate_supply = _CandidateSupply()
    worker._radio_supply = _RadioSupply()
    worker._artwork_supply = _ArtworkSupply()
    # This test isolates durable scheduling and supply ownership; composer
    # behavior is covered by the lifecycle suite.
    worker._handlers[HomeJobType.COMPOSE_FEED] = lambda _job: None

    results = worker.run_until_idle(max_jobs=12)
    jobs = repositories.jobs.list_for_scope("user-1")

    assert results
    assert {job.job_type for job in jobs} == {
        HomeJobType.REFRESH_CANDIDATES,
        HomeJobType.REFRESH_RADIO,
        HomeJobType.REPAIR_ARTWORK,
        HomeJobType.COMPOSE_FEED,
    }
    assert all(job.status is HomeJobStatus.SUCCEEDED for job in jobs)
    assert sum(job.job_type is HomeJobType.REFRESH_CANDIDATES for job in jobs) == 1
    assert sum(job.job_type is HomeJobType.REFRESH_RADIO for job in jobs) == 2
    assert sum(job.job_type is HomeJobType.REPAIR_ARTWORK for job in jobs) == 1


def test_initial_feed_composes_before_incomplete_radio_without_a_retry_loop(
    monkeypatch, tmp_path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "artwork-flood.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.worker.build_taste_profile",
        lambda _server, req: SimpleNamespace(
            user_scope_id=req.user_scope_id,
            profile_key="profile-1",
        ),
    )
    scheduler = HomeJobScheduler(object())
    scheduler.candidates(
        {"user_scope_id": "user-1"},
        reason="candidate_inventory_refresh",
        generation_hint="flood-cycle",
    )
    _seed_candidate(repositories, "candidate-ready")
    worker = HomeFeedWorker(object(), worker_id="flood-worker")
    worker._candidate_supply = _CandidateSupply()
    worker._radio_supply = _IncompleteFloodRadioSupply()
    execution_order: list[str] = []
    worker._handlers[HomeJobType.COMPOSE_FEED] = (
        lambda _job: execution_order.append("compose")
    )

    results = worker.run_until_idle(max_jobs=3)
    jobs = repositories.jobs.list_for_scope("user-1")
    artwork = [job for job in jobs if job.job_type is HomeJobType.REPAIR_ARTWORK]

    assert [result.job_type for result in results] == [
        HomeJobType.REFRESH_CANDIDATES.value,
        HomeJobType.COMPOSE_FEED.value,
        HomeJobType.REFRESH_RADIO.value,
    ]
    assert execution_order == ["compose"]
    assert len(artwork) == 5
    assert all(job.priority < 100 for job in artwork)


def test_worker_failure_uses_one_durable_retry_state(monkeypatch, tmp_path) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "retry.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.worker.build_taste_profile",
        lambda _server, req: SimpleNamespace(
            user_scope_id=req.user_scope_id,
            profile_key="profile-1",
        ),
    )
    scheduler = HomeJobScheduler(object())
    job = scheduler.radio(
        {"user_scope_id": "user-1"},
        reason="radio_supply_refresh",
        generation_hint="retry-cycle",
    )
    worker = HomeFeedWorker(object(), worker_id="retry-worker")
    worker._radio_supply = SimpleNamespace(
        refresh=lambda _taste: RadioSupplyOutcome(
            retryable=True,
            reason="provider_temporarily_unavailable",
        )
    )

    result = worker.run_once()
    persisted = repositories.jobs.get(job.job_id)

    assert result.outcome == "retry"
    assert persisted is not None
    assert persisted.status is HomeJobStatus.RETRY
    assert persisted.attempts == 1
    assert persisted.run_after > persisted.updated_at


def test_worker_renews_the_lease_while_a_handler_is_running(
    monkeypatch, tmp_path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "renew.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.worker.HOME_JOB_LEASE_SECONDS", 0.08
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.worker.HOME_JOB_LEASE_RENEW_SECONDS", 0.01
    )
    job = HomeJobScheduler(object()).compose(
        {"user_scope_id": "user-1"},
        reason="lease-test",
        candidate_snapshot_id="candidate-lease",
        generation_hint="lease-test",
    )
    renewals: list[float] = []
    original = repositories.jobs.renew_lease

    def renew(*args, **kwargs):
        renewals.append(time.time())
        return original(*args, **kwargs)

    monkeypatch.setattr(repositories.jobs, "renew_lease", renew)
    worker = HomeFeedWorker(object(), worker_id="lease-worker")
    worker._handlers[HomeJobType.COMPOSE_FEED] = lambda _job: time.sleep(0.05)

    result = worker.run_once()
    persisted = repositories.jobs.get(job.job_id)

    assert result.outcome == "succeeded"
    assert persisted is not None and persisted.status is HomeJobStatus.SUCCEEDED
    assert renewals
