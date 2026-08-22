from __future__ import annotations

import threading
import time
import pytest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from auralis_backend.discovery.config import FEED_PROMOTION_CONTRACT_VERSION, ROW_RECIPES
from auralis_backend.discovery import feed_state as feed_state_module
from auralis_backend.discovery.feed_state import (
    FeedState,
    artifact_promotion_contract,
    feed_queue_summary,
    invalidate_feed_state,
    load_feed_state,
    promote_prepared_feed,
    retain_compatible_ready_feeds,
    save_feed_state,
    store_active_feed,
    store_prepared_feed,
)
from auralis_backend.discovery.schema import DiscoveryArtifact, DiscoveryRow, TasteProfile
from auralis_backend.discovery.service import DiscoveryService
from auralis_backend.discovery.inventory import (
    CandidateInventory,
    load_candidate_inventory,
    store_candidate_inventory,
)
from auralis_backend.discovery.radio_inventory import ArtistRadioInventory
from auralis_backend.storage.artist_artwork import artist_artwork_token
from auralis_backend.contracts import SearchRequest


def _server(tmp_path):
    return SimpleNamespace(
        RECOMMENDATION_STORE_DB_PATH=str(tmp_path / "recommendation.sqlite3"),
        recommendation_store_lock=threading.RLock(),
    )


def test_queue_status_clears_expired_token_even_when_dirty(tmp_path) -> None:
    server = _server(tmp_path)
    state = FeedState(
        user_scope_id="stale-dirty",
        generation_status="dirty",
        dirty_reasons=["superseded"],
        preparation_lease_token="expired-token",
        preparation_lease_deadline=time.time() - 5,
        preparation_lease_reason="queue_replenish",
    )
    save_feed_state(server, state)
    service = DiscoveryService(server)
    service._queue_state_diagnostics(load_feed_state(server, "stale-dirty"))
    refreshed = load_feed_state(server, "stale-dirty")
    assert refreshed.preparation_lease_token == ""
    assert refreshed.generation_status == "delayed"


def test_search_request_preserves_launch_promotion_fields() -> None:
    request = SearchRequest(
        user_scope_id="scope",
        promote_ready_on_launch=True,
        launch_token="launch-1",
        feed_queue_status_only=True,
    )
    assert request.promote_ready_on_launch is True
    assert request.launch_token == "launch-1"
    assert request.feed_queue_status_only is True


def test_search_request_preserves_bounded_refresh_wait_field() -> None:
    request = SearchRequest(
        user_scope_id="scope",
        force_refresh=True,
        feed_refresh_wait_ms=3000,
    )
    assert request.feed_refresh_wait_ms == 3000


def test_delayed_retry_reason_survives_persistence(tmp_path) -> None:
    server = _server(tmp_path)
    state = FeedState(
        user_scope_id="retry-scope",
        generation_status="delayed",
        retry_at=time.time() - 1,
        retry_reason="radio_catalog_replenish",
    )
    assert save_feed_state(server, state)
    loaded = load_feed_state(server, "retry-scope")
    assert loaded is not None
    assert loaded.retry_reason == "radio_catalog_replenish"


def test_stale_preparation_lease_cannot_commit_successor(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "stale-preparation-lease"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        preparation_lease_token="new-token",
    )
    assert save_feed_state(server, state)
    result = store_prepared_feed(
        server,
        state,
        _artifact(scope, "stale", "track-stale"),
        expected_active_version=1,
        expected_preparation_lease_token="expired-token",
    )
    assert result.outcome == "version_race"
    assert result.reason == "preparation_lease_superseded"
    loaded = load_feed_state(server, scope)
    assert loaded is not None
    assert loaded.ready_feeds == []


def test_durable_two_feed_queue_overrides_stale_process_cache(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "durable-over-stale-cache"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
    )
    assert save_feed_state(server, state)
    assert (
        store_prepared_feed(
            server,
            state,
            _artifact(scope, "ready-a", "track-b"),
            expected_active_version=1,
        ).outcome
        == "stored"
    )
    stale = deepcopy(state)
    assert (
        store_prepared_feed(
            server,
            state,
            _artifact(scope, "ready-b", "track-c"),
            expected_active_version=1,
        ).outcome
        == "stored"
    )

    # Simulate another long-lived process retaining the state from before
    # Feed B was appended. SQLite must win after restart/cross-process reads.
    feed_state_module._STATE_CACHE[scope] = stale

    loaded = load_feed_state(server, scope)
    assert loaded is not None
    assert [entry.session_id for entry in loaded.ready_feeds] == [
        "ready-a",
        "ready-b",
    ]
    promoted = promote_prepared_feed(server, loaded)
    assert promoted is not None
    assert promoted.session_id == "ready-a"
    assert [entry.session_id for entry in loaded.ready_feeds] == ["ready-b"]


def test_stale_auxiliary_save_cannot_shrink_durable_queue(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "stale-auxiliary-writer"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
    )
    assert save_feed_state(server, state)
    assert store_prepared_feed(
        server,
        state,
        _artifact(scope, "ready-a", "track-b"),
        expected_active_version=1,
    ).outcome == "stored"
    stale = deepcopy(state)
    assert store_prepared_feed(
        server,
        state,
        _artifact(scope, "ready-b", "track-c"),
        expected_active_version=1,
    ).outcome == "stored"
    durable_revision = state.queue_revision

    stale.generation_status = "delayed"
    stale.retry_reason = "queue_replenish"
    assert save_feed_state(server, stale)
    assert [entry.session_id for entry in stale.ready_feeds] == [
        "ready-a",
        "ready-b",
    ]
    assert stale.queue_revision == durable_revision

    feed_state_module._STATE_CACHE[scope] = deepcopy(stale)
    loaded = load_feed_state(server, scope)
    assert loaded is not None
    assert [entry.session_id for entry in loaded.ready_feeds] == [
        "ready-a",
        "ready-b",
    ]


def test_two_ready_feeds_survive_reload_and_promote_one_at_a_time(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "restart-promotion-cycle"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
    )
    assert save_feed_state(server, state)
    for session_id, track_id in (("ready-a", "track-b"), ("ready-b", "track-c")):
        assert store_prepared_feed(
            server,
            state,
            _artifact(scope, session_id, track_id),
            expected_active_version=state.active_version,
        ).outcome == "stored"

    feed_state_module._STATE_CACHE.pop(scope, None)
    restarted = load_feed_state(server, scope)
    assert restarted is not None
    assert len(restarted.ready_feeds) == 2
    assert promote_prepared_feed(server, restarted).session_id == "ready-a"
    assert [entry.session_id for entry in restarted.ready_feeds] == ["ready-b"]

    feed_state_module._STATE_CACHE.pop(scope, None)
    restarted_again = load_feed_state(server, scope)
    assert restarted_again is not None
    assert promote_prepared_feed(server, restarted_again).session_id == "ready-b"
    assert restarted_again.ready_feeds == []


def test_direct_active_replacement_cannot_orphan_ready_chain(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "active-replacement-guard"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
    )
    assert save_feed_state(server, state)
    assert store_prepared_feed(
        server,
        state,
        _artifact(scope, "ready-a", "track-b"),
        expected_active_version=1,
    ).outcome == "stored"

    replaced = store_active_feed(
        server,
        state,
        _artifact(scope, "replacement", "track-z"),
        profile_fingerprint="profile-a",
        expected_active_version=1,
    )
    assert replaced is None
    loaded = load_feed_state(server, scope)
    assert loaded is not None
    assert loaded.active_feed.session_id == "active"
    assert [entry.session_id for entry in loaded.ready_feeds] == ["ready-a"]


def test_persistence_failure_is_not_reported_as_memory_success(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "persistence-failure"
    state = FeedState(user_scope_id=scope, generation_status="prepared")
    invalidate_feed_state(server, scope)
    with (
        patch.object(feed_state_module, "_persistent_set", return_value=False),
        patch.object(
            feed_state_module,
            "get_session_store",
            return_value=SimpleNamespace(),
        ),
    ):
        assert save_feed_state(server, state) is False
    assert scope not in feed_state_module._STATE_CACHE


def test_due_queue_status_rearms_exact_retry_reason_once(tmp_path, monkeypatch) -> None:
    server = _server(tmp_path)
    scope = "retry-rearm"
    assert save_feed_state(
        server,
        FeedState(
            user_scope_id=scope,
            generation_status="delayed",
            retry_at=time.time() - 1,
            retry_reason="radio_catalog_replenish",
        ),
    )
    service = DiscoveryService(server)
    scheduled: list[str] = []
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_taste_profile",
        lambda _server, _req: SimpleNamespace(
            user_scope_id=scope,
            profile_key="profile",
        ),
    )
    monkeypatch.setattr(
        service,
        "_schedule_preparation",
        lambda _req, _taste, *, reason: scheduled.append(reason),
    )
    request = SimpleNamespace(
        user_scope_id=scope,
        feed_queue_status_only=True,
        force_refresh=False,
        prefer_fresh_rows=False,
        session_intent=False,
    )
    service.recommend(request, request_mode="queue_status")
    assert scheduled == ["radio_catalog_replenish"]


def test_legacy_preparing_state_without_lease_becomes_retryable(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "legacy-stalled"
    state = FeedState(
        user_scope_id=scope,
        generation_status="preparing",
        dirty_reasons=["popular_radio_inventory_shortage"],
    )
    assert save_feed_state(server, state)
    service = DiscoveryService(server)
    diagnostics = service._queue_state_diagnostics(state)
    assert diagnostics["queue_build_inflight"] is False
    assert diagnostics["queue_phase"] == "delayed"
    assert diagnostics["retry_reason"] == "queue_replenish"
    assert diagnostics["retry_at"] > time.time()


def test_candidate_inventory_can_be_reused_for_new_profile_ranking(tmp_path) -> None:
    server = _server(tmp_path)
    now = time.time()
    inventory = CandidateInventory(
        user_scope_id="scope",
        profile_fingerprint="old-profile",
        generated_at=now,
        expires_at=now + 60,
        generation_id="inventory-1",
        coverage={"ready": True},
    )
    assert store_candidate_inventory(server, inventory)
    assert (
        load_candidate_inventory(
            server,
            "scope",
            profile_fingerprint="new-profile",
        )
        is None
    )
    reused = load_candidate_inventory(
        server,
        "scope",
        profile_fingerprint="new-profile",
        allow_profile_mismatch=True,
    )
    assert reused is not None
    assert reused.generation_id == "inventory-1"


def test_ready_inventory_preparation_executes_local_path_to_depth_two(
    tmp_path, monkeypatch
) -> None:
    server = _server(tmp_path)
    scope = "local-ready-execution"
    active = _artifact(scope, "active", "track-a", profile_key="profile")
    assert save_feed_state(
        server,
        FeedState(
            user_scope_id=scope,
            active_feed=active,
            active_version=1,
            profile_fingerprint="profile",
        ),
    )
    now = time.time()
    inventory = CandidateInventory(
        user_scope_id=scope,
        profile_fingerprint="profile",
        generated_at=now,
        expires_at=now + 600,
        generation_id="candidate-ready",
        coverage={"ready": True},
    )
    assert store_candidate_inventory(server, inventory)
    service = DiscoveryService(server)
    taste = TasteProfile(
        user_scope_id=scope,
        profile_key="profile",
        signal_tier="personalized",
        full_history_tracks=[],
        top_artists=[],
        artist_hints=[],
    )
    request = SimpleNamespace(user_scope_id=scope, refresh_token="")

    class ExecuteExecutor:
        def submit(self, callback):
            callback()

    service._prepare_executor = ExecuteExecutor()
    ready_radio = ArtistRadioInventory(
        user_scope_id=scope,
        profile_fingerprint="profile",
        generated_at=now,
        expires_at=now + 600,
        generation_id="radio-ready",
        cards=[
                {
                    "id": f"radio-{index}",
                    "thumbnail": (
                        f"/artist_artwork/{artist_artwork_token(f'provider:artist:radio-{index}')}"
                    ),
                    "collage_images": [
                        f"/artist_artwork/{artist_artwork_token(f'provider:artist:radio-{index}')}"
                    ],
                    "tracks": [{"id": f"radio-{index}-track-{track}"} for track in range(12)],
                    "seed_artist_identity": f"provider:artist:radio-{index}",
                    "seed_artist_identity_token": artist_artwork_token(
                        f"provider:artist:radio-{index}"
                    ),
                    "artwork_owner_identity": f"provider:artist:radio-{index}",
                    "artwork_owner_token": artist_artwork_token(
                        f"provider:artist:radio-{index}"
                    ),
                }
                for index in range(12)
            ],
        reservoir_cards=[],
        diagnostics={"reservoir_size": 24},
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.load_artist_radio_inventory",
        lambda *_args, **_kwargs: ready_radio,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.select_radio_rotation",
        lambda inventory, **_kwargs: inventory,
    )
    expansion_depths = []
    monkeypatch.setattr(
        service,
        "_schedule_radio_reservoir_expansion",
        lambda *_args, **_kwargs: expansion_depths.append(
            len((load_feed_state(server, scope) or FeedState(scope)).ready_feeds)
        ) or False,
    )
    built_artifacts = iter(
        [
            _artifact(scope, "prepared-1", "track-b", profile_key="profile"),
            _artifact(scope, "prepared-2", "track-c", profile_key="profile"),
        ]
    )
    monkeypatch.setattr(
        service,
        "_build_artifact",
        lambda *_args, **_kwargs: next(built_artifacts),
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.hydrate_artifact_release_metadata",
        lambda _server, artifact, **_kwargs: (artifact, 0),
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.candidate_inventory_coverage",
        lambda *_args, **_kwargs: {
            "ready": True,
            "actual": {"unique_tracks": 160},
            "minimums": {"unique_tracks": 120},
            "failed_contracts": [],
        },
    )
    for name in (
        "materialize_enrichment_plan",
        "verify_materialized_supply",
        "store_candidate_inventory",
    ):
        monkeypatch.setattr(
            f"auralis_backend.discovery.service.{name}",
            lambda *_args, _name=name, **_kwargs: (_ for _ in ()).throw(
                AssertionError(f"{_name} must not run on local path")
            ),
        )
    monkeypatch.setattr(
        service,
        "_schedule_radio_artwork_repairs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("artwork provider scheduling must not run")
        ),
    )
    started = time.perf_counter()
    service._schedule_preparation(request, taste, reason="queue_replenish")
    elapsed = time.perf_counter() - started
    state = load_feed_state(server, scope)
    assert state is not None
    assert len(state.ready_feeds) == 2
    assert all(
        entry.artifact.diagnostics.get("local_only_path") is True
        and entry.artifact.diagnostics.get("provider_network_work_count") == 0
        for entry in state.ready_feeds
    )
    assert expansion_depths == [2]
    assert elapsed < 10.0
    persisted = load_candidate_inventory(server, scope, profile_fingerprint="profile")
    assert persisted is not None
    assert persisted.generation_id == "candidate-ready"


def test_local_ready_replenishment_without_radio_stays_radio_specific(
    tmp_path, monkeypatch
) -> None:
    server = _server(tmp_path)
    scope = "local-radio-shortage"
    now = time.time()
    assert save_feed_state(
        server,
        FeedState(
            user_scope_id=scope,
            active_feed=_artifact(scope, "active", "track-a", profile_key="profile"),
            active_version=1,
            profile_fingerprint="profile",
        ),
    )
    assert store_candidate_inventory(
        server,
        CandidateInventory(
            user_scope_id=scope,
            profile_fingerprint="profile",
            generated_at=now,
            expires_at=now + 600,
            generation_id="candidate-ready",
            coverage={"ready": True},
        ),
    )
    service = DiscoveryService(server)

    class ExecuteExecutor:
        def submit(self, callback):
            callback()

    class CaptureTimer:
        callbacks = []

        def __init__(self, _interval, callback):
            self.callback = callback
            self.daemon = False

        def start(self):
            self.callbacks.append(self.callback)

    service._prepare_executor = ExecuteExecutor()
    monkeypatch.setattr(
        "auralis_backend.discovery.service.threading.Timer", CaptureTimer
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.load_artist_radio_inventory",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_artist_radio_inventory",
        lambda *_args, **_kwargs: ArtistRadioInventory(
            user_scope_id=scope,
            profile_fingerprint="profile",
            generated_at=now,
            expires_at=now + 600,
            generation_id="radio-shortage",
            cards=[],
            reservoir_cards=[],
            diagnostics={"reservoir_shortage": True},
        ),
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.candidate_inventory_coverage",
        lambda *_args, **_kwargs: {
            "ready": True,
            "actual": {"unique_tracks": 160},
            "minimums": {"unique_tracks": 120},
            "failed_contracts": [],
        },
    )
    for name in (
        "materialize_enrichment_plan",
        "verify_materialized_supply",
        "store_candidate_inventory",
    ):
        monkeypatch.setattr(
            f"auralis_backend.discovery.service.{name}",
            lambda *_args, _name=name, **_kwargs: (_ for _ in ()).throw(
                AssertionError(f"{_name} must not run on local shortage path")
            ),
        )
    monkeypatch.setattr(
        service,
        "_schedule_radio_artwork_repairs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("artwork repair must not overlap local preparation")
        ),
    )
    monkeypatch.setattr(
        service,
        "_build_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a strict radio shortage must not publish a feed")
        ),
    )
    taste = TasteProfile(
        user_scope_id=scope,
        profile_key="profile",
        signal_tier="personalized",
        full_history_tracks=[],
        top_artists=[],
        artist_hints=[],
    )
    service._schedule_preparation(
        SimpleNamespace(user_scope_id=scope, refresh_token=""),
        taste,
        reason="queue_replenish",
    )
    state = load_feed_state(server, scope)
    assert state is not None
    assert state.ready_feeds == []
    assert state.generation_status == "delayed"
    assert state.retry_reason == "radio_catalog_replenish"
    assert state.preparation_lease_token == ""
    assert len(CaptureTimer.callbacks) == 1
    persisted = load_candidate_inventory(server, scope, profile_fingerprint="profile")
    assert persisted is not None and persisted.generation_id == "candidate-ready"


def test_promotion_contract_rejects_legacy_radio_but_accepts_verified_successor() -> None:
    artifact = _artifact("scope", "candidate", "track-b")
    assert artifact_promotion_contract(artifact) == (True, "ready")
    artifact.diagnostics.pop("feed_promotion_contract", None)
    valid, reason = artifact_promotion_contract(artifact)
    assert valid is False
    assert reason == "promotion_contract_outdated"


def _artifact(
    scope: str,
    session_id: str,
    track_id: str,
    *,
    profile_key: str = "profile-a",
    include_optional_rows: bool = True,
) -> DiscoveryArtifact:
    now = time.time()
    rows = [
        DiscoveryRow(
            id="todays_pick",
            title="Today's Pick",
            kind="todays_pick",
            item_type="track",
            items=[{"id": track_id, "title": track_id, "artist": "Artist"}],
        )
    ]
    if include_optional_rows:
        for kind in (
            "featured_new_albums",
            "popular_radio",
            "recommended_albums",
        ):
            rows.append(
                DiscoveryRow(
                    id=kind,
                    title=kind,
                    kind=kind,
                    item_type="radio" if kind == "popular_radio" else "album",
                    items=[
                        {
                            "id": f"{session_id}-{kind}-{index}",
                            "thumbnail": (
                                f"/artist_artwork/{index:032x}"
                                if kind == "popular_radio"
                                else f"https://images.example/{index}.jpg"
                            ),
                            "collage_images": (
                                [f"/artist_artwork/{index:032x}"]
                                if kind == "popular_radio"
                                else []
                            ),
                            "tracks": (
                                [{"id": f"{session_id}-{kind}-{index}-{track}"} for track in range(12)]
                                if kind == "popular_radio"
                                else []
                            ),
                        }
                        for index in range(ROW_RECIPES[kind].min_items)
                    ],
                )
            )
    return DiscoveryArtifact(
        session_id=session_id,
        user_scope_id=scope,
        profile_key=profile_key,
        generated_at=now,
        expires_at=now + 3600,
        rows=rows,
        diagnostics={"feed_promotion_contract": FEED_PROMOTION_CONTRACT_VERSION},
        candidate_pool_counts={},
        provider_timings_ms={},
        home_tab_lanes={},
        accepted=True,
    )


def test_ready_queue_persists_and_promotes_fifo_with_entry_metadata(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "queue-fifo"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=3,
        active_inventory_generation="inventory-a",
        rotation_epoch=8,
        profile_fingerprint="profile-a",
    )
    try:
        assert save_feed_state(server, state)
        first = _artifact(scope, "ready-b", "track-b")
        second = _artifact(scope, "ready-c", "track-c")
        assert store_prepared_feed(
            server,
            state,
            first,
            expected_active_version=3,
            expected_inventory_generation="inventory-a",
            inventory_generation="inventory-b",
            rotation_epoch=9,
            intent_version=4,
        )
        assert store_prepared_feed(
            server,
            state,
            second,
            expected_active_version=3,
            expected_inventory_generation="inventory-a",
            inventory_generation="inventory-c",
            rotation_epoch=10,
            intent_version=5,
        )

        with patch.object(feed_state_module, "_STATE_CACHE", {}):
            loaded = load_feed_state(server, scope)
        assert loaded is not None
        assert [entry.session_id for entry in loaded.ready_feeds] == [
            "ready-b",
            "ready-c",
        ]
        assert loaded.ready_feeds[0].inventory_generation == "inventory-b"
        assert loaded.ready_feeds[1].intent_version == 5

        promoted = promote_prepared_feed(server, loaded)
        assert promoted is not None and promoted.session_id == "ready-b"
        assert loaded.active_inventory_generation == "inventory-b"
        assert loaded.rotation_epoch == 9
        assert [entry.session_id for entry in loaded.ready_feeds] == ["ready-c"]

        promoted = promote_prepared_feed(server, loaded)
        assert promoted is not None and promoted.session_id == "ready-c"
        assert loaded.active_inventory_generation == "inventory-c"
        assert loaded.rotation_epoch == 10
        assert loaded.ready_feeds == []
    finally:
        invalidate_feed_state(server, scope)


def test_ready_queue_rejects_duplicate_or_third_successor(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "queue-capacity"
    active = _artifact(scope, "active", "track-a")
    state = FeedState(
        user_scope_id=scope,
        active_feed=active,
        active_version=1,
        profile_fingerprint="profile-a",
    )
    try:
        assert save_feed_state(server, state)
        first = _artifact(scope, "ready-b", "track-b")
        assert store_prepared_feed(server, state, first, expected_active_version=1)
        duplicate = _artifact(scope, "ready-b-copy", "track-b")
        duplicate.rows = first.rows
        duplicate_result = store_prepared_feed(
            server,
            state,
            duplicate,
            expected_active_version=1,
        )
        assert duplicate_result.outcome == "duplicate"
        assert duplicate_result.reason == "content_duplicate"
        third_result = store_prepared_feed(
            server,
            state,
            _artifact(scope, "ready-c", "track-c"),
            expected_active_version=1,
        )
        assert third_result.outcome == "stored"
        full_result = store_prepared_feed(
            server,
            state,
            _artifact(scope, "ready-d", "track-d"),
            expected_active_version=1,
        )
        assert full_result.outcome == "queue_full"
        assert [entry.session_id for entry in full_result.ready_feeds] == [
            "ready-b",
            "ready-c",
        ]
    finally:
        invalidate_feed_state(server, scope)


def test_ready_queue_never_weakens_completed_optional_rows(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "queue-quality"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        profile_fingerprint="profile-a",
    )
    try:
        assert save_feed_state(server, state)
        weak = _artifact(
            scope,
            "weak-successor",
            "track-b",
            include_optional_rows=False,
        )
        result = store_prepared_feed(
            server,
            state,
            weak,
            expected_active_version=1,
        )
        assert result.outcome == "contract_shortage"
        assert result.reason == "popular_radio_card_shortage"
        loaded = load_feed_state(server, scope)
        assert loaded is not None and loaded.ready_feeds == []
        assert loaded.active_feed is not None
        assert loaded.active_feed.session_id == "active"
    finally:
        invalidate_feed_state(server, scope)


def test_ready_queue_accepts_contract_saturated_nested_depth_delta(tmp_path) -> None:
    """Nested reserve depth is capped at the contract minimum (97 vs 99 is visible-safe)."""
    server = _server(tmp_path)
    scope = "queue-depth-delta"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        profile_fingerprint="profile-a",
    )
    try:
        assert save_feed_state(server, state)
        first = _artifact(scope, "ready-a", "track-b")
        second = _artifact(scope, "ready-b", "track-c")
        for artifact, depth in ((first, 99), (second, 97)):
            radio = next(row for row in artifact.rows if row.kind == "popular_radio")
            radio.items[0]["tracks"] = [
                {"id": f"nested-{index}"} for index in range(depth)
            ]
        assert store_prepared_feed(server, state, first, expected_active_version=1).outcome == "stored"
        result = store_prepared_feed(server, state, second, expected_active_version=1)
        assert result.outcome == "stored"
        assert len(result.ready_feeds) == 2
    finally:
        invalidate_feed_state(server, scope)


def test_promotion_retains_parent_chain_and_allows_replenishment(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "queue-parent-chain"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        profile_fingerprint="profile-a",
    )
    try:
        assert save_feed_state(server, state)
        first = _artifact(scope, "ready-a", "track-b")
        second = _artifact(scope, "ready-b", "track-c")
        assert store_prepared_feed(server, state, first, expected_active_version=1).outcome == "stored"
        assert store_prepared_feed(server, state, second, expected_active_version=1).outcome == "stored"
        loaded = load_feed_state(server, scope)
        assert loaded is not None
        assert loaded.ready_feeds[0].parent_session_id == "active"
        assert loaded.ready_feeds[1].parent_session_id == "ready-a"
        assert promote_prepared_feed(server, loaded) is not None
        assert loaded.active_feed is not None and loaded.active_feed.session_id == "ready-a"
        assert [entry.session_id for entry in loaded.ready_feeds] == ["ready-b"]
        assert loaded.ready_feeds[0].parent_session_id == "ready-a"
        replenished = store_prepared_feed(
            server,
            loaded,
            _artifact(scope, "ready-c", "track-d"),
            expected_active_version=loaded.active_version,
        )
        assert replenished.outcome == "stored"
        assert [entry.session_id for entry in replenished.ready_feeds] == ["ready-b", "ready-c"]
        assert replenished.ready_feeds[1].parent_session_id == "ready-b"
    finally:
        invalidate_feed_state(server, scope)


def test_invalid_head_is_discarded_without_orphaning_tail(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "queue-invalid-head"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        profile_fingerprint="profile-a",
    )
    try:
        assert save_feed_state(server, state)
        assert store_prepared_feed(server, state, _artifact(scope, "ready-a", "track-b"), expected_active_version=1).outcome == "stored"
        assert store_prepared_feed(server, state, _artifact(scope, "ready-b", "track-c"), expected_active_version=1).outcome == "stored"
        loaded = load_feed_state(server, scope)
        assert loaded is not None
        loaded.ready_feeds[0].artifact.accepted = False
        assert save_feed_state(
            server,
            loaded,
            expected_active_version=loaded.active_version,
            expected_ready_session_ids=[
                entry.session_id for entry in loaded.ready_feeds
            ],
        )
        promoted = promote_prepared_feed(server, loaded)
        assert promoted is None
        assert loaded.ready_feeds == []
        assert loaded.active_feed is not None and loaded.active_feed.session_id == "active"
    finally:
        invalidate_feed_state(server, scope)


def test_artwork_only_change_has_distinct_content_signature(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "queue-artwork-signature"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        profile_fingerprint="profile-a",
    )
    try:
        assert save_feed_state(server, state)
        first = _artifact(scope, "ready-a", "track-b")
        second = deepcopy(first)
        second.session_id = "ready-b"
        for left_row, right_row in zip(first.rows, second.rows):
            for left, right in zip(left_row.items, right_row.items):
                right["id"] = left.get("id")
                right["thumbnail"] = left.get("thumbnail")
        second.rows[0].items[0]["thumbnail"] = "https://images.example/artwork-only.jpg"
        assert store_prepared_feed(server, state, first, expected_active_version=1).outcome == "stored"
        result = store_prepared_feed(server, state, second, expected_active_version=1)
        assert result.outcome == "stored"
        assert len(result.ready_feeds) == 2
    finally:
        invalidate_feed_state(server, scope)


def test_duplicate_recovery_is_persisted_and_bounded(tmp_path, monkeypatch) -> None:
    server = _server(tmp_path)
    scope = "queue-recovery-once"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        profile_fingerprint="profile-a",
    )
    assert save_feed_state(server, state)
    service = DiscoveryService(server)
    scheduled: list[str] = []
    monkeypatch.setattr(service, "_schedule_preparation", lambda *_a, **kwargs: scheduled.append(kwargs["reason"]))
    taste = SimpleNamespace(user_scope_id=scope)
    assert service._schedule_queue_recovery_once(
        SimpleNamespace(), taste, state,
        outcome="duplicate", reason="queue_novelty_delta", inventory_generation="inventory-a",
    ) is True
    assert service._schedule_queue_recovery_once(
        SimpleNamespace(), taste, state,
        outcome="duplicate", reason="queue_novelty_delta", inventory_generation="inventory-a",
    ) is False
    assert scheduled == ["queue_novelty_delta"]
    persisted = load_feed_state(server, scope)
    assert persisted is not None
    assert persisted.recovery_attempt_keys == ["duplicate:active:inventory-a"]
    invalidate_feed_state(server, scope)


def test_profile_change_discards_only_incompatible_successors(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "queue-profile"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        profile_fingerprint="profile-a",
    )
    try:
        assert save_feed_state(server, state)
        assert store_prepared_feed(
            server,
            state,
            _artifact(scope, "ready-a", "track-b", profile_key="profile-a"),
            expected_active_version=1,
        )
        assert store_prepared_feed(
            server,
            state,
            _artifact(scope, "ready-b", "track-c", profile_key="profile-b"),
            expected_active_version=1,
        )
        retained = retain_compatible_ready_feeds(server, state, "profile-b")
        # ready-b was built from ready-a. Dropping its parent invalidates the
        # dependent tail even if the tail's own profile fingerprint matches.
        assert retained.ready_feeds == []
        assert retained.active_feed is not None
        assert retained.active_feed.session_id == "active"
    finally:
        invalidate_feed_state(server, scope)


def test_search_return_keeps_active_and_pull_promotes_one_ready_feed(
    tmp_path,
    monkeypatch,
) -> None:
    server = _server(tmp_path)
    scope = "queue-lifecycle"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        active_inventory_generation="inventory-a",
        profile_fingerprint="profile-a",
    )
    service = DiscoveryService(server)
    scheduled: list[str] = []
    monkeypatch.setattr(
        service,
        "_schedule_preparation_after_response",
        lambda _req, *, reason, dedupe_key="": scheduled.append(reason),
    )
    try:
        assert save_feed_state(server, state)
        assert store_prepared_feed(
            server,
            state,
            _artifact(scope, "ready-b", "track-b"),
            expected_active_version=1,
            inventory_generation="inventory-b",
            rotation_epoch=2,
        )
        assert store_prepared_feed(
            server,
            state,
            _artifact(scope, "ready-c", "track-c"),
            expected_active_version=1,
            inventory_generation="inventory-c",
            rotation_epoch=3,
        )

        search_response = service.recommend(
            SimpleNamespace(
                user_scope_id=scope,
                force_refresh=False,
                prefer_fresh_rows=False,
                session_intent=True,
            ),
            request_mode="full_feed",
        )
        after_search = load_feed_state(server, scope)
        assert search_response["feed_action"] == "served_active"
        assert after_search is not None
        assert after_search.active_feed is not None
        assert after_search.active_feed.session_id == "active"
        assert [entry.session_id for entry in after_search.ready_feeds] == [
            "ready-b",
            "ready-c",
        ]

        pull_response = service.recommend(
            SimpleNamespace(
                user_scope_id=scope,
                force_refresh=True,
                prefer_fresh_rows=False,
                # A queued weak Search signal may share this request. It must
                # shape replenishment without suppressing pull promotion.
                session_intent=True,
            ),
            request_mode="full_feed",
        )
        after_pull = load_feed_state(server, scope)
        assert pull_response["feed_action"] == "promoted_prepared"
        assert pull_response["diagnostics"]["ready_feed_count"] == 1
        assert after_pull is not None and after_pull.active_feed is not None
        assert after_pull.active_feed.session_id == "ready-b"
        assert [entry.session_id for entry in after_pull.ready_feeds] == ["ready-c"]
        assert scheduled == ["search_session_intent", "post_promotion"]
    finally:
        invalidate_feed_state(server, scope)


def test_explicit_launch_promotes_fifo_once_and_retains_tail(tmp_path, monkeypatch) -> None:
    server = _server(tmp_path)
    scope = "queue-launch"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        profile_fingerprint="profile-a",
    )
    service = DiscoveryService(server)
    monkeypatch.setattr(
        service,
        "_schedule_preparation_after_response",
        lambda *_args, **_kwargs: None,
    )
    try:
        assert save_feed_state(server, state)
        assert store_prepared_feed(
            server, state, _artifact(scope, "ready-b", "track-b"), expected_active_version=1
        )
        assert store_prepared_feed(
            server, state, _artifact(scope, "ready-c", "track-c"), expected_active_version=1
        )
        req = SimpleNamespace(
            user_scope_id=scope,
            force_refresh=False,
            prefer_fresh_rows=False,
            session_intent=False,
            promote_ready_on_launch=True,
            launch_token="launch-1",
        )
        first = service.recommend(req, request_mode="full_feed")
        assert first["feed_action"] == "promoted_prepared"
        current = load_feed_state(server, scope)
        assert current is not None and current.active_feed.session_id == "ready-b"
        assert [entry.session_id for entry in current.ready_feeds] == ["ready-c"]

        # Duplicate launch delivery with the same token is idempotent.
        second = service.recommend(req, request_mode="full_feed")
        assert second["feed_action"] == "served_active"
        current = load_feed_state(server, scope)
        assert current is not None and current.active_feed.session_id == "ready-b"
        assert [entry.session_id for entry in current.ready_feeds] == ["ready-c"]
    finally:
        invalidate_feed_state(server, scope)


def test_background_prepare_only_schedules_status(tmp_path, monkeypatch) -> None:
    server = _server(tmp_path)
    scope = "queue-background-status"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        profile_fingerprint="profile-a",
    )
    assert save_feed_state(server, state)
    service = DiscoveryService(server)
    monkeypatch.setattr(service, "_schedule_preparation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        service,
        "_build_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("sync build")),
    )
    try:
        response = service.recommend(
            SimpleNamespace(
                user_scope_id=scope,
                force_refresh=False,
                session_intent=False,
                recent_queries=[],
                taste_queries=[],
                query="",
                limit=20,
                session_id="",
                artist_hints=[],
                album_hints=[],
                avoid_ids=[],
                recent_track_ids=[],
                top_track_ids=[],
                artist_ids=[],
                playlist_names=[],
                library_track_ids=[],
                offline_track_ids=[],
            ),
            request_mode="background_prepare",
        )
        assert response["feed_action"] == "served_active"
    finally:
        invalidate_feed_state(server, scope)


def test_queue_status_does_not_build_schedule_or_promote(tmp_path, monkeypatch) -> None:
    server = _server(tmp_path)
    scope = "queue-status-only"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=2,
        profile_fingerprint="profile-a",
    )
    assert save_feed_state(server, state)
    assert store_prepared_feed(server, state, _artifact(scope, "ready", "track-b"), expected_active_version=2)
    service = DiscoveryService(server)
    monkeypatch.setattr("auralis_backend.discovery.service.build_taste_profile", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("taste")))
    monkeypatch.setattr(service, "_schedule_preparation", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("schedule")))
    monkeypatch.setattr(service, "_store_session", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("store session")))
    monkeypatch.setattr("auralis_backend.discovery.service.promote_prepared_feed", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("promote")))
    response = service.recommend(SimpleNamespace(user_scope_id=scope, feed_queue_status_only=True), request_mode="queue_status")
    assert response["diagnostics"]["ready_feed_depth"] == 1
    assert response["diagnostics"]["ready_feed_target_depth"] == 2
    assert response["diagnostics"]["user_scope_id"] == scope
    assert response["rows"] == []


def test_queue_status_revision_wait_wakes_on_append(tmp_path, monkeypatch) -> None:
    server = _server(tmp_path)
    scope = "queue-status-revision-wake"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        generation_status="preparing",
        preparation_lease_token="lease-1",
        preparation_lease_started_at=time.time(),
        preparation_lease_deadline=time.time() + 10,
        preparation_lease_reason="queue_replenish",
    )
    assert save_feed_state(server, state)
    revision = feed_queue_summary(server, scope)["queue_revision"]
    service = DiscoveryService(server)
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_taste_profile",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("taste")),
    )
    monkeypatch.setattr(
        service,
        "_schedule_preparation",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("schedule")),
    )

    def append() -> None:
        time.sleep(0.06)
        current = load_feed_state(server, scope) or state
        result = store_prepared_feed(
            server,
            current,
            _artifact(scope, "ready", "track-b"),
            expected_active_version=1,
        )
        assert result.outcome == "stored"

    worker = threading.Thread(target=append)
    worker.start()
    started = time.perf_counter()
    response = service.recommend(
        SimpleNamespace(
            user_scope_id=scope,
            feed_queue_status_only=True,
            feed_queue_revision=revision,
            feed_queue_wait_ms=500,
        ),
        request_mode="queue_status",
    )
    elapsed = time.perf_counter() - started
    worker.join(timeout=1)

    diagnostics = response["diagnostics"]
    assert diagnostics["ready_feed_depth"] == 1
    assert diagnostics["queue_revision"] > revision
    assert diagnostics["queue_status_waited"] is True
    assert diagnostics["queue_status_timed_out"] is False
    assert elapsed < 0.5


def test_queue_status_revision_wait_times_out_without_build_side_effect(
    tmp_path, monkeypatch
) -> None:
    server = _server(tmp_path)
    scope = "queue-status-revision-timeout"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        generation_status="preparing",
        preparation_lease_token="lease-1",
        preparation_lease_started_at=time.time(),
        preparation_lease_deadline=time.time() + 10,
        preparation_lease_reason="queue_replenish",
    )
    assert save_feed_state(server, state)
    revision = feed_queue_summary(server, scope)["queue_revision"]
    service = DiscoveryService(server)
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_taste_profile",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("taste")),
    )
    monkeypatch.setattr(
        service,
        "_schedule_preparation",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("schedule")),
    )

    response = service.recommend(
        SimpleNamespace(
            user_scope_id=scope,
            feed_queue_status_only=True,
            feed_queue_revision=revision,
            feed_queue_wait_ms=60,
        ),
        request_mode="queue_status",
    )

    diagnostics = response["diagnostics"]
    assert diagnostics["ready_feed_depth"] == 0
    assert diagnostics["queue_revision"] == revision
    assert diagnostics["queue_status_waited"] is True
    assert diagnostics["queue_status_timed_out"] is True


def test_queue_status_summary_does_not_hydrate_artifacts(tmp_path, monkeypatch) -> None:
    server = _server(tmp_path)
    scope = "queue-status-metadata-only"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
    )
    assert save_feed_state(server, state)
    assert store_prepared_feed(
        server,
        state,
        _artifact(scope, "ready", "track-b"),
        expected_active_version=1,
    ).outcome == "stored"
    monkeypatch.setattr(
        feed_state_module,
        "_state_from_payload",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("hydrate")),
    )

    summary = feed_queue_summary(server, scope)

    assert summary["ready_feed_depth"] == 1
    assert summary["ready_feed_session_ids"] == ["ready"]


def test_queue_topology_changes_serialize_only_new_artifact_bodies(
    tmp_path, monkeypatch
) -> None:
    server = _server(tmp_path)
    scope = "queue-new-artifact-only"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
    )
    assert save_feed_state(server, state)
    serialized_sessions = []
    original = feed_state_module.artifact_to_dict

    def record(artifact):
        serialized_sessions.append(str(artifact.session_id or ""))
        return original(artifact)

    monkeypatch.setattr(feed_state_module, "artifact_to_dict", record)
    assert store_prepared_feed(
        server,
        state,
        _artifact(scope, "ready-a", "track-b"),
        expected_active_version=1,
    ).outcome == "stored"
    assert serialized_sessions == ["ready-a"]

    serialized_sessions.clear()
    assert store_prepared_feed(
        server,
        state,
        _artifact(scope, "ready-b", "track-c"),
        expected_active_version=1,
    ).outcome == "stored"
    assert serialized_sessions == ["ready-b"]

    serialized_sessions.clear()
    assert promote_prepared_feed(server, state) is not None
    assert serialized_sessions == []


def test_full_ready_queue_skips_preparation_before_claim_and_reconciliation(tmp_path, monkeypatch) -> None:
    server = _server(tmp_path)
    scope = "queue-full-noop"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
    )
    assert save_feed_state(server, state)
    assert store_prepared_feed(server, state, _artifact(scope, "ready-a", "track-b"), expected_active_version=1).outcome == "stored"
    assert store_prepared_feed(server, state, _artifact(scope, "ready-b", "track-c"), expected_active_version=1).outcome == "stored"
    service = DiscoveryService(server)
    monkeypatch.setattr(service, "_claim_background_build", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("claim")))
    monkeypatch.setattr("auralis_backend.discovery.service.build_taste_profile", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("taste")))
    taste = SimpleNamespace(user_scope_id=scope, profile_key="profile")
    service._schedule_preparation(SimpleNamespace(user_scope_id=scope), taste, reason="queue_replenish")
    service._schedule_preparation_after_response(SimpleNamespace(user_scope_id=scope), reason="release_metadata_replenish", dedupe_key="metadata")
    current = load_feed_state(server, scope)
    assert current is not None
    assert len(current.ready_feeds) == 2
    assert current.generation_status != "preparing"


def test_full_ready_queue_does_not_clear_active_lease(tmp_path, monkeypatch) -> None:
    server = _server(tmp_path)
    scope = "queue-full-active-lease"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        generation_status="preparing",
        preparation_lease_started_at=100.0,
        preparation_lease_deadline=200.0,
        preparation_lease_reason="queue_replenish",
    )
    assert save_feed_state(server, state)
    assert store_prepared_feed(server, state, _artifact(scope, "ready-a", "track-b"), expected_active_version=1).outcome == "stored"
    assert store_prepared_feed(server, state, _artifact(scope, "ready-b", "track-c"), expected_active_version=1).outcome == "stored"
    service = DiscoveryService(server)
    service._schedule_preparation_after_response(
        SimpleNamespace(user_scope_id=scope), reason="post_promotion"
    )
    current = load_feed_state(server, scope)
    assert current is not None
    assert current.preparation_lease_reason == "queue_replenish"
    assert current.preparation_lease_deadline == 200.0


def test_release_metadata_enrichment_does_not_claim_feed_builder(
    tmp_path, monkeypatch
) -> None:
    server = _server(tmp_path)
    scope = "metadata-off-feed-builder"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        generation_status="prepared",
    )
    assert save_feed_state(server, state)
    service = DiscoveryService(server)
    scheduled = []
    monkeypatch.setattr(
        service,
        "_schedule_release_metadata_enrichment",
        lambda req, taste: scheduled.append((req, taste)),
    )
    monkeypatch.setattr(
        service,
        "_claim_background_build",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("metadata must not claim feed builder")
        ),
    )

    req = SimpleNamespace(user_scope_id=scope)
    taste = SimpleNamespace(user_scope_id=scope, profile_key="profile")
    service._schedule_preparation(req, taste, reason="release_metadata_replenish")

    assert scheduled == [(req, taste)]
    assert service._metadata_executor is not service._prepare_executor
    current = load_feed_state(server, scope)
    assert current is not None
    assert current.generation_status == "prepared"


def test_legacy_stale_prepared_slot_is_not_migrated_into_queue(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "queue-stale-legacy"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        prepared_feed=_artifact(scope, "stale", "track-b"),
        active_version=4,
        prepared_base_version=3,
        profile_fingerprint="profile-a",
    )
    try:
        assert state.prepared_feed is None
        assert state.ready_feeds == []
        assert state.generation_status == "stale_prepared_discarded"
        assert save_feed_state(server, state)
        assert promote_prepared_feed(server, state) is None
    finally:
        invalidate_feed_state(server, scope)


def test_bounded_refresh_wait_promotes_when_successor_arrives(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "queue-bounded-wait"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        profile_fingerprint="profile-a",
    )
    assert save_feed_state(server, state)
    service = DiscoveryService(server)

    def append() -> None:
        time.sleep(0.08)
        current = load_feed_state(server, scope) or state
        store_prepared_feed(
            server,
            current,
            _artifact(scope, "ready", "track-b"),
            expected_active_version=1,
        )

    worker = threading.Thread(target=append)
    worker.start()
    waited_state, promoted, changed, elapsed = service._wait_for_prepared_feed(
        scope,
        state,
        request_id="test",
        wait_ms=500,
    )
    worker.join(timeout=1)
    assert promoted is not None
    assert changed is True
    assert waited_state.active_feed is not None
    assert waited_state.active_feed.session_id == "ready"
    assert elapsed < 500
    invalidate_feed_state(server, scope)


def test_bounded_refresh_wait_times_out_without_successor(tmp_path) -> None:
    server = _server(tmp_path)
    scope = "queue-bounded-timeout"
    state = FeedState(
        user_scope_id=scope,
        active_feed=_artifact(scope, "active", "track-a"),
        active_version=1,
        profile_fingerprint="profile-a",
    )
    assert save_feed_state(server, state)
    service = DiscoveryService(server)
    waited_state, promoted, changed, elapsed = service._wait_for_prepared_feed(
        scope,
        state,
        request_id="test",
        wait_ms=40,
    )
    assert promoted is None
    assert changed is False
    assert waited_state.active_feed is not None
    assert elapsed >= 30
    invalidate_feed_state(server, scope)
