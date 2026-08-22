from __future__ import annotations

import json
import time
import threading
from types import SimpleNamespace
from unittest.mock import patch

from auralis_backend.api.media_runtime import get_album_details
from auralis_backend.domain import user_state as user_state_module
from auralis_backend.domain.server_adapter import adapt_domain_server
from auralis_backend.discovery.allocation import allocate_home_rows
from auralis_backend.discovery.adapters import row_page_response_from_artifact
from auralis_backend.discovery.artifact import evaluate_quality, row_contract_report
from auralis_backend.discovery import enrichment as enrichment_module
from auralis_backend.discovery.enrichment import (
    EnrichmentRequest,
    MaterializedCandidateSupply,
    build_enrichment_plan,
    complete_inventory_release_metadata,
    hydrate_artifact_release_metadata,
)
from auralis_backend.discovery.config import ROW_RECIPES
from auralis_backend.discovery.feed_state import (
    FeedState,
    invalidate_feed_state,
    load_feed_state,
    promote_prepared_feed,
    save_feed_state,
    store_prepared_feed,
)
from auralis_backend.discovery.inventory import (
    CandidateInventory,
    build_candidate_inventory,
    refresh_candidate_inventory_coverage,
    store_candidate_inventory,
)
from auralis_backend.discovery.radio_inventory import build_artist_radio_inventory
from auralis_backend.discovery.ranking import (
    build_personal_mixes,
    build_rows_from_pools,
    rank_tracks,
)
from auralis_backend.discovery.schema import (
    DiscoveryArtifact,
    DiscoveryCandidate,
    DiscoveryRow,
    TasteProfile,
)
from auralis_backend.discovery.service import (
    DiscoveryService,
    _completed_optional_rows,
    _preserve_complete_optional_rows,
)
from auralis_backend.discovery.structured_providers import (
    CanonicalRecording,
    LastFmClient,
    ListenBrainzClient,
)
from auralis_backend.recommend.store_runtime import open_recommendation_store_connection


class _Server:
    @staticmethod
    def _recommendation_trim_text(value):
        return str(value or "").strip()

    @staticmethod
    def normalize_recommendation_track(raw):
        return dict(raw)


def test_history_event_snapshot_preserves_canonical_and_playback_identity() -> None:
    snapshot = user_state_module._snapshot_from_event_payload(
        track_id="recording:canonical-track",
        artist_name="Canonical Artist",
        payload={
            "track_key": "recording:canonical-track",
            "title": "Canonical Song",
            "artist": "Canonical Artist",
            "thumbnail": "",
            "musicbrainz_recording_id": "canonical-track",
            "playback": {
                "provider": "youtube",
                "source_id": "Y0000000042",
            },
        },
    )

    assert snapshot is not None
    assert snapshot["track_key"] == "recording:canonical-track"
    assert snapshot["videoId"] == "Y0000000042"
    assert snapshot["playback_source_id"] == "Y0000000042"
    assert snapshot["musicbrainz_recording_id"] == "canonical-track"


def _track(index: int, *, relation: str, artist_offset: int = 0) -> dict:
    artist_index = (index + artist_offset) % 24
    recording_id = f"00000000-0000-4000-8000-{index + artist_offset:012d}"
    source_id = f"T{index + artist_offset:010d}"[-11:]
    return {
        "id": source_id,
        "videoId": source_id,
        "track_key": f"recording:{recording_id}",
        "musicbrainz_recording_id": recording_id,
        "title": f"Canonical unplayed track {index + artist_offset}",
        "artist": f"Canonical artist {artist_index}",
        "channel": f"Canonical artist {artist_index} - Topic",
        "album": f"Canonical album {(index + artist_offset) % 48}",
        "duration": 220,
        "genre": "rock",
        "language": "english",
        "language_confidence": 0.95,
        "region": "global",
        "region_confidence": 0.9,
        "playable": True,
        "playback_verified": True,
        "source_provider": "youtube",
        "playback_source_id": source_id,
        "source_authority": "topic",
        "source_identity_confidence": 0.96,
        "materialized_relation": relation,
        "recommendation_path": relation,
        "playback": {
            "provider": "youtube",
            "source_id": source_id,
            "authority": "topic",
        },
    }


def _taste() -> TasteProfile:
    history = [
        {
            "id": f"history-{index}",
            "track_key": f"recording:history-{index}",
            "title": f"Played {index}",
            "artist": f"Canonical artist {index}",
            "album": f"Played album {index}",
            "genre": "rock",
            "language": "english",
            "source_authority": "official",
            "play_count": 20 - index,
            "last_played_at": 1000 - index,
        }
        for index in range(8)
    ]
    return TasteProfile(
        user_scope_id="structured-test",
        profile_key="structured-test-profile",
        signal_tier="established",
        recent_tracks=list(history),
        top_tracks=list(history),
        anchor_tracks=list(history),
        last_played_tracks=list(history),
        full_history_tracks=list(history),
        frequent_tracks=list(history),
        artist_hints=[track["artist"] for track in history],
        top_artists=[track["artist"] for track in history],
        source_profile={
            "top_genres": ["rock"],
            "accepted_languages": ["english"],
        },
    )


def _feed_artifact(user_scope: str, session_id: str, item_id: str) -> DiscoveryArtifact:
    now = time.time()
    return DiscoveryArtifact(
        session_id=session_id,
        user_scope_id=user_scope,
        profile_key="profile-a",
        generated_at=now,
        expires_at=now + 3600,
        rows=[
            DiscoveryRow(
                "todays_pick",
                "Today's Pick",
                "todays_pick",
                "track",
                [{"id": item_id, "title": item_id, "artist": "Band A"}],
            )
        ],
        diagnostics={},
        candidate_pool_counts={},
        provider_timings_ms={},
        home_tab_lanes={},
        accepted=True,
    )


def _optional_row(kind: str, count: int) -> DiscoveryRow:
    return DiscoveryRow(
        kind,
        kind.replace("_", " ").title(),
        kind,
        "album" if "album" in kind else "radio",
        [{"id": f"{kind}-{index}"} for index in range(count)],
    )


def test_structured_supply_clears_core_candidate_shortage_without_optional_rows() -> None:
    supply = MaterializedCandidateSupply(
        pools={
            "profile_spine": [_track(index, relation="same_artist_catalog") for index in range(96)],
            "similarity": [_track(index, relation="track_radio", artist_offset=200) for index in range(72)],
            "artist_graph": [_track(index, relation="artist_neighbor", artist_offset=400) for index in range(72)],
            "genre_mood": [_track(index, relation="structured_tag", artist_offset=600) for index in range(48)],
        }
    )
    with patch(
        "auralis_backend.search.catalog_pipeline.catalog_playable_backbone_tracks",
        return_value=[],
    ):
        inventory = build_candidate_inventory(
            _Server(),
            _taste(),
            materialized_supply=supply,
        )

    assert inventory.is_ready, inventory.coverage
    assert inventory.coverage["failed_contracts"] == []
    assert inventory.coverage["actual"]["unplayed_tracks"] >= 120
    assert "popular_radio" not in inventory.coverage["minimums"]
    assert "featured_new_albums" not in inventory.coverage["minimums"]
    assert "recommended_albums" not in inventory.coverage["minimums"]


def test_structured_inventory_builds_a_complete_core_feed() -> None:
    supply = MaterializedCandidateSupply(
        pools={
            "profile_spine": [_track(index, relation="same_artist_catalog") for index in range(100)],
            "similarity": [_track(index, relation="track_radio", artist_offset=200) for index in range(80)],
            "artist_graph": [_track(index, relation="artist_neighbor", artist_offset=400) for index in range(80)],
            "genre_mood": [_track(index, relation="structured_tag", artist_offset=600) for index in range(60)],
        }
    )
    with patch(
        "auralis_backend.search.catalog_pipeline.catalog_playable_backbone_tracks",
        return_value=[],
    ):
        inventory = build_candidate_inventory(_Server(), _taste(), materialized_supply=supply)
    rows, _status, _lanes, diagnostics = build_rows_from_pools(inventory.pools, _taste())
    accepted, reasons, _quality = evaluate_quality(
        rows=rows,
        taste=_taste(),
        home_tab_diagnostics=diagnostics,
    )

    assert accepted, reasons
    counts = {row.kind: len(row.items) for row in rows}
    assert counts["todays_pick"] >= 6
    assert 5 <= counts["made_for_you"] <= 12
    assert counts["because_you_played"] >= 12
    assert counts["recommended_artists"] >= 10
    assert counts["quiet_picks"] >= 20
    assert "popular_radio" not in counts


def test_made_for_you_uses_flexible_pools_and_reserves_five_complete_mixes() -> None:
    supply = MaterializedCandidateSupply(
        pools={
            "profile_spine": [
                _track(index, relation="same_artist_catalog") for index in range(32)
            ],
            "similarity": [
                _track(index, relation="track_radio", artist_offset=200)
                for index in range(24)
            ],
        }
    )
    taste = _taste()
    with patch(
        "auralis_backend.search.catalog_pipeline.catalog_playable_backbone_tracks",
        return_value=[],
    ):
        inventory = build_candidate_inventory(
            _Server(), taste, materialized_supply=supply
        )
    mixes, diagnostics = build_personal_mixes(inventory.pools, taste)

    def identity(track):
        return str(
            track.get("canonical_entity_id")
            or track.get("canonical_track_identity")
            or track.get("track_key")
            or track.get("id")
            or ""
        )

    visible_ids = [identity(track) for mix in mixes for track in mix["tracks"][:8]]
    history_ids = {str(track["id"]) for track in taste.full_history_tracks}
    discovery_mixes = [mix for mix in mixes if mix["id"] != "picked_again"]
    picked_again = next(mix for mix in mixes if mix["id"] == "picked_again")

    assert 5 <= len(mixes) <= 12
    assert all(len(mix["tracks"]) >= 8 for mix in mixes)
    assert len(visible_ids) == 40
    assert len(set(visible_ids)) == 40
    assert all(
        not ({str(track.get("id") or "") for track in mix["tracks"]} & history_ids)
        for mix in discovery_mixes
    )
    assert {str(track.get("id") or "") for track in picked_again["tracks"]} & history_ids
    assert diagnostics["discovery_mix_unplayed_ratio"] >= 0.70
    assert inventory.coverage["actual"]["made_for_you_mix_count"] == 5
    assert inventory.coverage["actual"]["made_for_you_tracks"] == 40

    prior_ids = set()
    for mix in mixes:
        ids = [identity(track) for track in mix["tracks"]]
        overlap = len(set(ids[8:]) & prior_ids)
        assert overlap / max(len(ids), 1) <= 0.20
        prior_ids.update(ids)

    rows, _status, _lanes, _diagnostics = build_rows_from_pools(
        inventory.pools, taste
    )
    final_row = next(row for row in rows if row.kind == "made_for_you")
    assert 5 <= len(final_row.items) <= 12
    assert all(len(item.get("tracks") or []) >= 8 for item in final_row.items)


def test_made_for_you_readiness_rejects_a_large_but_non_diverse_pool() -> None:
    tracks = []
    for index in range(45):
        track = _track(index, relation="same_artist_catalog", artist_offset=700)
        track["artist"] = "Canonical artist 0"
        track["channel"] = "Canonical artist 0 - Topic"
        tracks.append(track)
    with patch(
        "auralis_backend.search.catalog_pipeline.catalog_playable_backbone_tracks",
        return_value=[],
    ):
        inventory = build_candidate_inventory(
            _Server(),
            _taste(),
            materialized_supply=MaterializedCandidateSupply(
                pools={"profile_spine": tracks}
            ),
        )

    assert inventory.coverage["actual"]["unplayed_tracks"] >= 40
    assert inventory.coverage["actual"]["made_for_you_mix_count"] < 5
    assert inventory.coverage["actual"]["made_for_you_tracks"] < 40
    assert "made_for_you_tracks" in inventory.coverage["failed_contracts"]


def test_enrichment_plan_emits_only_structured_provider_jobs() -> None:
    plan = build_enrichment_plan(_taste())
    kinds = {request.kind for request in plan.requests}
    assert kinds <= {
        "lastfm_track_similar",
        "listenbrainz_artist_recordings",
        "canonical_artist_radio_catalog",
        "lastfm_artist_similar",
        "canonical_album_catalog",
        "lastfm_tag_tracks",
        "listenbrainz_user_recommendations",
        "listenbrainz_sitewide_recordings",
    }
    assert not any(
        phrase in request.key.casefold()
        for request in plan.requests
        for phrase in (" top songs", " official audio", " songs")
    )


def test_related_artist_work_is_split_into_small_saved_slices() -> None:
    plan = build_enrichment_plan(_taste())
    requests = [
        request for request in plan.requests if request.kind == "lastfm_artist_similar"
    ]
    by_seed = {}
    for request in requests:
        by_seed.setdefault(request.key, []).append(request)

    assert by_seed
    assert all(
        {int(request.metadata["offset"]) for request in seed_requests} == {0, 3}
        for seed_requests in by_seed.values()
    )
    assert all(request.limit == 8 for request in requests)


def test_bounded_scheduler_reserves_album_work_without_growing_the_batch() -> None:
    requests = [
        *[
            EnrichmentRequest("fixture", str(index), "similarity", "fixture", 1)
            for index in range(8)
        ],
        EnrichmentRequest("fixture", "profile", "profile_spine", "fixture", 1),
        EnrichmentRequest("fixture", "artist", "artist_graph", "fixture", 1),
        EnrichmentRequest("fixture", "genre", "genre_mood", "fixture", 1),
        EnrichmentRequest("fixture", "album", "album", "fixture", 1),
    ]

    selected = enrichment_module._select_scheduled_requests(requests, 5)

    assert [request.pool for request in selected] == [
        "album",
        "similarity",
        "profile_spine",
        "artist_graph",
        "genre_mood",
    ]
    assert len(selected) == 5


def test_completed_radio_is_independent_from_incomplete_album_shelves() -> None:
    completed = _completed_optional_rows(
        {
            "actual": {
                "popular_radio": 9,
                "featured_new_albums": 5,
                "recommended_albums": 3,
            }
        }
    )

    assert completed == {"popular_radio"}


def test_complete_active_radio_is_kept_until_its_replacement_is_complete() -> None:
    active = _feed_artifact("radio-preserve", "active", "active-track")
    active.rows.append(_optional_row("popular_radio", 8))
    replacement = _feed_artifact("radio-preserve", "replacement", "new-track")

    rows, preserved = _preserve_complete_optional_rows(
        replacement.rows,
        active,
    )

    assert preserved == ["popular_radio"]
    assert len(next(row for row in rows if row.kind == "popular_radio").items) == 8


def test_album_plan_prioritizes_underrepresented_and_related_artists() -> None:
    taste = _taste()
    saturated = taste.top_artists[0]
    adjacent = "Adjacent Canonical Artist"
    plan = build_enrichment_plan(
        taste,
        acquisition_ledger={
            "album_shelf_shortages": {
                "featured_new_albums": 3,
                "recommended_albums": 9,
            },
            "album_artist_counts": {
                " ".join(artist.casefold().split()): 3
                for artist in taste.top_artists
            },
            "album_expansion_artist_seeds": [adjacent],
        },
    )
    album_requests = [
        request for request in plan.requests if request.kind == "canonical_album_catalog"
    ]

    assert album_requests
    assert album_requests[0].metadata["profile_seed_artist"] == adjacent
    assert album_requests[0].metadata["album_artist_qualified_count"] == 0
    assert next(
        request
        for request in album_requests
        if request.metadata["profile_seed_artist"] == saturated
    ).metadata["album_artist_qualified_count"] == 3


def test_completed_radio_releases_its_jobs_to_remaining_album_work() -> None:
    plan = build_enrichment_plan(
        _taste(),
        acquisition_ledger={
            "optional_row_counts": {
                "popular_radio": 9,
                "featured_new_albums": 5,
                "recommended_albums": 3,
            },
            "album_shelf_shortages": {
                "featured_new_albums": 3,
                "recommended_albums": 9,
            },
        },
    )

    assert not any(
        request.kind == "canonical_artist_radio_catalog"
        for request in plan.requests
    )
    assert any(request.kind == "canonical_album_catalog" for request in plan.requests)


def test_album_acquisition_continues_until_post_filter_reserve_is_full() -> None:
    plan = build_enrichment_plan(
        _taste(),
        acquisition_ledger={
            "failed_domains": ["quiet_picks"],
            "album_shelf_shortages": {
                "featured_new_albums": 0,
                "recommended_albums": 0,
            },
            "qualified_album_reserve_shortage": 12,
        },
    )

    assert any(request.kind == "canonical_album_catalog" for request in plan.requests)


def test_repeated_pull_does_not_discard_completed_prepared_feed(monkeypatch) -> None:
    user_scope = f"feed-refresh-coalesce-{int(time.time() * 1000000)}"
    active = _feed_artifact(user_scope, "coalesce-active", "active-track")
    prepared = _feed_artifact(user_scope, "coalesce-prepared", "prepared-track")
    inventory = CandidateInventory(
        user_scope_id=user_scope,
        profile_fingerprint="profile-a",
        generated_at=time.time(),
        expires_at=time.time() + 3600,
        generation_id="coalesce-inventory",
        coverage={
            "ready": True,
            "actual": {"featured_new_albums": 8},
            "failed_contracts": [],
        },
    )
    state = FeedState(
        user_scope_id=user_scope,
        active_feed=active,
        active_version=4,
        active_inventory_generation=inventory.generation_id,
        profile_fingerprint="profile-a",
        generation_status="ready",
    )
    taste = TasteProfile(
        user_scope_id=user_scope,
        profile_key="profile-a",
        signal_tier="known",
        recent_tracks=[{"id": "seed", "artist": "Band A"}],
    )
    request = SimpleNamespace(refresh_token="pull-one")
    service = DiscoveryService(object())

    monkeypatch.setattr(
        "auralis_backend.discovery.service.load_candidate_inventory",
        lambda *_args, **_kwargs: inventory,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.refresh_candidate_inventory_coverage",
        lambda current, **_kwargs: current,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.schedule_catalog_population",
        lambda *_args, **_kwargs: {"reason": "already_complete"},
    )

    def build_with_repeated_pull(*_args, **_kwargs):
        service._pending_background_builds[user_scope] = (
            request,
            taste,
            "background_prepare",
        )
        return prepared

    monkeypatch.setattr(service, "_build_artifact", build_with_repeated_pull)
    try:
        assert save_feed_state(None, state) is True
        service._schedule_preparation(request, taste, reason="pull_to_refresh")
        service._prepare_executor.shutdown(wait=True)
        loaded = load_feed_state(None, user_scope)
    finally:
        invalidate_feed_state(None, user_scope)

    assert loaded is not None
    assert loaded.prepared_feed is not None
    assert loaded.prepared_feed.session_id == prepared.session_id


def test_more_complete_optional_rows_append_after_an_older_ready_feed() -> None:
    user_scope = f"prepared-optional-{int(time.time() * 1000000)}"
    active = _feed_artifact(user_scope, "optional-active", "active-track")
    older = _feed_artifact(user_scope, "optional-older", "older-track")
    older.rows.extend(
        [
            _optional_row("featured_new_albums", 8),
            _optional_row("popular_radio", 8),
            _optional_row("recommended_albums", 10),
        ]
    )
    replacement = _feed_artifact(user_scope, "optional-replacement", "replacement-track")
    replacement.rows.extend(
        [
            _optional_row("featured_new_albums", 8),
            _optional_row("popular_radio", 8),
            _optional_row("recommended_albums", 12),
        ]
    )
    state = FeedState(
        user_scope_id=user_scope,
        active_feed=active,
        prepared_feed=older,
        active_version=4,
        prepared_base_version=4,
        profile_fingerprint="profile-a",
        generation_status="prepared",
    )

    try:
        assert save_feed_state(None, state) is True
        stored = store_prepared_feed(
            None,
            state,
            replacement,
            expected_active_version=4,
        )
        loaded = load_feed_state(None, user_scope)
    finally:
        invalidate_feed_state(None, user_scope)

    assert stored is not None
    assert loaded is not None and loaded.prepared_feed is not None
    assert [entry.session_id for entry in loaded.ready_feeds] == [
        "optional-older",
        "optional-replacement",
    ]
    promoted = promote_prepared_feed(None, loaded)
    assert promoted is not None and promoted.session_id == "optional-older"
    assert loaded.prepared_feed is not None
    assert loaded.prepared_feed.session_id == "optional-replacement"


def test_feed_state_persists_small_artifact_references_and_loads_both_feeds(tmp_path) -> None:
    server = SimpleNamespace(
        RECOMMENDATION_STORE_DB_PATH=str(tmp_path / "recommendation.sqlite3"),
        recommendation_store_lock=threading.RLock(),
    )
    user_scope = "compact-feed-state"
    active = _feed_artifact(user_scope, "compact-active", "active-track")
    prepared = _feed_artifact(user_scope, "compact-prepared", "prepared-track")
    state = FeedState(
        user_scope_id=user_scope,
        active_feed=active,
        prepared_feed=prepared,
        active_version=7,
        prepared_base_version=7,
        profile_fingerprint="profile-a",
        generation_status="prepared",
    )

    assert save_feed_state(server, state) is True
    connection = open_recommendation_store_connection(server)
    try:
        row = connection.execute(
            """
            SELECT payload_json
            FROM recommendation_feature_store
            WHERE namespace = 'discovery_feed_state_v2'
              AND entity_id = ?
            """,
            [f"feed-state:{user_scope}"],
        ).fetchone()
        artifact_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM recommendation_feature_store
            WHERE namespace = 'discovery_feed_artifact'
              AND entity_id LIKE ?
            """,
            [f"{user_scope}:%"],
        ).fetchone()[0]
    finally:
        connection.close()

    payload = json.loads(row["payload_json"])
    assert "active_feed" not in payload
    assert "prepared_feed" not in payload
    assert payload["active_feed_session_id"] == "compact-active"
    assert payload["prepared_feed_session_id"] == "compact-prepared"
    assert artifact_count == 2

    with patch("auralis_backend.discovery.feed_state._STATE_CACHE", {}):
        loaded = load_feed_state(server, user_scope)
    assert loaded is not None
    assert loaded.active_feed is not None
    assert loaded.active_feed.session_id == "compact-active"
    assert loaded.prepared_feed is not None
    assert loaded.prepared_feed.session_id == "compact-prepared"


def test_active_launch_returns_without_rebuilding_taste_profile() -> None:
    user_scope = f"launch-fast-path-{int(time.time() * 1000000)}"
    state = FeedState(
        user_scope_id=user_scope,
        active_feed=_feed_artifact(user_scope, "launch-active", "active-track"),
        prepared_feed=_feed_artifact(user_scope, "launch-prepared", "prepared-track"),
        active_version=3,
        prepared_base_version=3,
        profile_fingerprint="profile-a",
        generation_status="prepared",
    )
    service = DiscoveryService(object())
    request = SimpleNamespace(
        user_scope_id=user_scope,
        force_refresh=False,
        prefer_fresh_rows=False,
        session_intent=False,
    )

    try:
        assert save_feed_state(None, state) is True
        with patch(
            "auralis_backend.discovery.service.build_taste_profile",
            side_effect=AssertionError("active launch must not rebuild taste"),
        ):
            response = service._recommend_v2(
                request,
                request_mode="full_feed",
            )
    finally:
        invalidate_feed_state(None, user_scope)
        service._prepare_executor.shutdown(wait=True)

    assert response["rows"]
    assert response["diagnostics"]["feed_action"] == "served_active"


def test_history_summary_is_loaded_without_rescanning_events(tmp_path) -> None:
    server = SimpleNamespace(
        RECOMMENDATION_STORE_DB_PATH=str(tmp_path / "recommendation.sqlite3"),
        recommendation_store_lock=threading.RLock(),
    )
    scope = "history-summary-user"
    seed = {
        "recent_track_ids": ["recording:one"],
        "recent_track_snapshots": [
            {
                "track_key": "recording:one",
                "title": "Prepared history track",
                "artist": "Prepared artist",
            }
        ],
        "history_track_snapshots": [],
        "frequent_track_snapshots": [],
        "_diagnostics": {"persisted_event_count": 42},
    }
    domain_server = adapt_domain_server(server)

    assert user_state_module._store_history_snapshot(domain_server, scope, seed) is True
    with user_state_module._HISTORY_SNAPSHOT_LOCK:
        user_state_module._HISTORY_SNAPSHOT_CACHE.pop(scope, None)
    with patch.object(
        user_state_module,
        "_build_scope_history_seed_from_events",
        side_effect=AssertionError("persisted summary must avoid an event rescan"),
    ):
        first = user_state_module._load_scope_history_seed(domain_server, scope)
        second = user_state_module._load_scope_history_seed(domain_server, scope)

    assert first["recent_track_ids"] == ["recording:one"]
    assert first["_diagnostics"]["history_snapshot_source"] == "persistent"
    assert second["_diagnostics"]["history_snapshot_source"] == "memory"


def test_pull_without_prepared_feed_schedules_background_work_without_inline_build() -> None:
    user_scope = f"refresh-fast-path-{int(time.time() * 1000000)}"
    active = _feed_artifact(user_scope, "refresh-active", "active-track")
    state = FeedState(
        user_scope_id=user_scope,
        active_feed=active,
        active_version=2,
        profile_fingerprint="profile-a",
        generation_status="ready",
    )
    taste = TasteProfile(
        user_scope_id=user_scope,
        profile_key="profile-a",
        signal_tier="known",
        recent_tracks=[{"id": "seed", "artist": "Band A"}],
    )
    service = DiscoveryService(object())
    request = SimpleNamespace(
        user_scope_id=user_scope,
        force_refresh=True,
        prefer_fresh_rows=True,
        session_intent=False,
        refresh_token="refresh-fast-path",
    )

    try:
        assert save_feed_state(None, state) is True
        with patch(
            "auralis_backend.discovery.service.build_taste_profile",
            return_value=taste,
        ), patch.object(service, "_schedule_preparation") as schedule, patch.object(
            service,
            "_build_artifact",
            side_effect=AssertionError("refresh must not build inline"),
        ):
            response = service._recommend_v2(
                request,
                request_mode="full_feed",
            )
    finally:
        invalidate_feed_state(None, user_scope)
        service._prepare_executor.shutdown(wait=True)

    schedule.assert_called_once()
    assert response["rows"]
    assert response["diagnostics"]["feed_action"] == "unchanged_no_rotation"
    assert response["diagnostics"]["feed_action_reason"] == "refresh_preparing_successor"


def test_playable_album_inventory_persists_detail_tracklist_and_artwork(tmp_path) -> None:
    server = SimpleNamespace(
        RECOMMENDATION_STORE_DB_PATH=str(tmp_path / "recommendation.sqlite3"),
        recommendation_store_lock=threading.RLock(),
    )
    release_group_id = "23eafc30-3978-37fb-bfa0-8058aa1eb81a"
    tracks = [_track(index, relation="album") for index in range(8)]
    album = DiscoveryCandidate(
        item={
            "id": f"musicbrainz:release-group:{release_group_id}",
            "title": "Prepared canonical album",
            "artist": "Canonical artist 0",
            "musicbrainz_release_group_id": release_group_id,
            "playable": True,
            "tracks": tracks,
        },
        source="album",
        score=8.0,
        reasons=["canonical_album_catalog"],
        item_type="album",
    )
    inventory = CandidateInventory(
        user_scope_id="album-detail-test",
        profile_fingerprint="profile-a",
        generated_at=time.time(),
        expires_at=time.time() + 3600,
        pools={"album": [album]},
        generation_id="album-detail-inventory",
    )

    assert store_candidate_inventory(server, inventory) is True
    details = get_album_details(
        server,
        f"musicbrainz:release-group:{release_group_id}",
    )

    assert details["track_count"] == 8
    assert len(details["tracks"]) == 8
    assert details["thumbnail"].startswith("https://i.ytimg.com/vi/")
    assert all(track["thumbnail"] for track in details["tracks"])


def test_same_refresh_token_observes_running_build_without_queueing_another() -> None:
    service = DiscoveryService(object())
    taste = TasteProfile(
        user_scope_id="refresh-token-user",
        profile_key="profile-a",
        signal_tier="known",
    )
    fingerprint = service._background_fingerprint(taste)
    try:
        assert service._claim_background_build(
            fingerprint,
            urgent=True,
            refresh_token="pull-one",
        )
        service._schedule_preparation(
            SimpleNamespace(refresh_token="pull-one"),
            taste,
            reason="pull_to_refresh",
        )
        assert fingerprint not in service._pending_background_builds

        service._schedule_preparation(
            SimpleNamespace(refresh_token="pull-two"),
            taste,
            reason="pull_to_refresh",
        )
        assert service._pending_background_builds[fingerprint][0].refresh_token == "pull-two"
    finally:
        service._release_background_build(fingerprint)
        service._prepare_executor.shutdown(wait=False, cancel_futures=True)


def test_scheduler_reserves_three_jobs_for_independent_radio_catalogs() -> None:
    requests = [
        *[
            EnrichmentRequest(
                "canonical_artist_radio_catalog",
                f"artist-{index}",
                "radio_artist_catalog",
                "same_artist_catalog",
                48,
            )
            for index in range(8)
        ],
        *[
            EnrichmentRequest("fixture", str(index), "similarity", "fixture", 1)
            for index in range(8)
        ],
    ]

    selected = enrichment_module._select_scheduled_requests(requests, 6)

    assert sum(request.pool == "radio_artist_catalog" for request in selected) == 3
    assert len(selected) == 6


def test_popular_radio_builds_eight_cards_only_from_its_owned_catalog() -> None:
    taste = _taste()
    candidates = []
    for seed_index, seed_artist in enumerate(taste.top_artists[:8]):
        for track_index in range(24):
            track = _track(
                seed_index * 100 + track_index,
                relation="same_artist_catalog",
                artist_offset=1000,
            )
            track["radio_seed_artist"] = seed_artist
            track["related_to_artist"] = seed_artist
            track["artist"] = seed_artist if track_index < 15 else f"Neighbor {seed_index}-{track_index % 3}"
            candidates.append(
                DiscoveryCandidate(
                    item=track,
                    source="radio_artist_catalog",
                    score=6.0,
                    reasons=["same_artist_catalog"],
                    item_type="track",
                )
            )

    inventory = build_artist_radio_inventory(
        taste,
        {"radio_artist_catalog": candidates},
    )

    assert len(inventory.cards) == 8
    assert all(len(card["tracks"]) == 24 for card in inventory.cards)
    assert all(card["seed_affinity"] == "direct" for card in inventory.cards)


def test_popular_radio_accepts_twelve_track_cards_and_keeps_the_deeper_target() -> None:
    taste = _taste()
    candidates = []
    for seed_index, seed_artist in enumerate(taste.top_artists[:8]):
        for track_index in range(12):
            track = _track(
                seed_index * 100 + track_index,
                relation="same_artist_catalog",
                artist_offset=1000,
            )
            track["radio_seed_artist"] = seed_artist
            track["related_to_artist"] = seed_artist
            track["artist"] = (
                seed_artist if track_index < 8 else f"Neighbor {seed_index}"
            )
            track["artist_thumbnail"] = (
                f"https://artists.test/{seed_index}/seed.jpg"
                if track_index < 8
                else f"https://artists.test/{seed_index}/neighbor.jpg"
            )
            candidates.append(
                DiscoveryCandidate(
                    item=track,
                    source="radio_artist_catalog",
                    score=6.0,
                    reasons=["same_artist_catalog"],
                    item_type="track",
                )
            )

    inventory = build_artist_radio_inventory(
        taste,
        {"radio_artist_catalog": candidates},
    )

    assert inventory.is_ready is True
    assert len(inventory.cards) == 8
    assert all(len(card["tracks"]) == 12 for card in inventory.cards)
    assert all(len(card["collage_images"]) == 2 for card in inventory.cards)
    assert all(len(set(card["collage_images"])) == 2 for card in inventory.cards)
    assert inventory.diagnostics["minimum_tracks_per_card"] == 12
    assert inventory.diagnostics["target_tracks_per_card"] == 24


def test_twelve_track_radio_cards_survive_allocation_and_artifact_validation() -> None:
    cards = []
    for card_index in range(8):
        tracks = [
            _track(
                card_index * 100 + track_index,
                relation="same_artist_catalog",
                artist_offset=2000,
            )
            for track_index in range(12)
        ]
        cards.append(
            {
                "id": f"radio-{card_index}",
                "title": f"Radio {card_index}",
                "tracks": tracks,
                "items": tracks,
            }
        )
    row = DiscoveryRow(
        id="popular_radio",
        kind="popular_radio",
        title="Popular Radio",
        item_type="radio",
        items=cards,
    )

    allocated, _diagnostics = allocate_home_rows([row], _taste())
    contracts = row_contract_report(
        rows=allocated,
        taste=_taste(),
        home_tab_diagnostics={},
    )

    assert len(allocated[0].items) == 8
    assert all(len(card["tracks"]) == 12 for card in allocated[0].items)
    assert contracts["contracts"]["popular_radio"] is True
    assert contracts["contracts"]["popular_radio_card_depth"] is True


def test_related_artist_slice_fetches_only_its_three_neighbors() -> None:
    class _LastFm:
        requested_limits = []

        def __init__(self, _server):
            pass

        def similar_artists(self, _artist, *, artist_mbid="", limit=12):
            self.__class__.requested_limits.append(limit)
            return [
                {
                    "artist": f"Neighbor {index}",
                    "musicbrainz_artist_id": f"neighbor-{index}",
                    "relationship_score": 1.0 - index / 10,
                }
                for index in range(limit)
            ]

    class _ListenBrainz:
        requested_artists = []

        def __init__(self, _server):
            pass

        def top_recordings(self, artist_mbid, *, limit=30):
            self.__class__.requested_artists.append(artist_mbid)
            return [
                {
                    "title": f"{artist_mbid} track {index}",
                    "artist": artist_mbid,
                    "musicbrainz_recording_id": f"{artist_mbid}-recording-{index}",
                }
                for index in range(limit)
            ]

    request = EnrichmentRequest(
        kind="lastfm_artist_similar",
        key="seed-artist",
        pool="artist_graph",
        relation="artist_neighbor",
        limit=8,
        metadata={
            "profile_seed_artist": "Seed Artist",
            "musicbrainz_artist_id": "seed-mbid",
            "offset": 3,
        },
    )
    with patch.object(enrichment_module, "LastFmClient", _LastFm), patch.object(
        enrichment_module, "ListenBrainzClient", _ListenBrainz
    ):
        rows = enrichment_module._fetch_request(object(), request)

    assert _LastFm.requested_limits == [6]
    assert _ListenBrainz.requested_artists == ["neighbor-3", "neighbor-4", "neighbor-5"]
    assert len(rows) == 8
    assert {row["neighbor_artist"] for row in rows} == {
        "Neighbor 3",
        "Neighbor 4",
        "Neighbor 5",
    }


def test_lastfm_retries_unmapped_recording_mbid_with_exact_artist_and_title() -> None:
    client = LastFmClient(api_key="test-key")
    recording = CanonicalRecording(
        title="Example",
        artist="Example Artist",
        recording_mbid="00000000-0000-4000-8000-000000000001",
    )
    response = {
        "similartracks": {
            "track": [
                {
                    "name": "Neighbour",
                    "artist": {"name": "Neighbour Artist"},
                    "match": "0.91",
                }
            ]
        }
    }
    with patch.object(
        client,
        "_call",
        side_effect=[RuntimeError("lastfm_6:Track not found"), response],
    ) as call:
        rows = client.similar_tracks(recording)

    assert len(rows) == 1
    assert call.call_args_list[0].kwargs["mbid"] == recording.recording_mbid
    assert call.call_args_list[1].kwargs["track"] == recording.title
    assert call.call_args_list[1].kwargs["artist"] == recording.artist


def test_listenbrainz_metadata_batches_without_dropping_recordings() -> None:
    client = ListenBrainzClient()
    mbids = [f"00000000-0000-4000-8000-{index:012d}" for index in range(120)]
    batch_sizes = []

    def fake_get(_url, *, params=None):
        batch = str((params or {}).get("recording_mbids") or "").split(",")
        batch_sizes.append(len(batch))
        return {
            mbid: {
                "recording": {"name": f"Track {mbid}", "length": 180000},
                "artist": {
                    "name": "Canonical artist",
                    "artists": [{"artist_mbid": "artist-mbid"}],
                },
                "release": {"name": "Canonical album", "mbid": "release-mbid"},
            }
            for mbid in batch
        }

    with patch.object(client, "get", side_effect=fake_get):
        metadata = client.recording_metadata(mbids)

    assert batch_sizes == [50, 50, 20]
    assert list(metadata) == mbids
    assert all(value["artist"] == "Canonical artist" for value in metadata.values())


def test_listenbrainz_personal_recommendations_use_batch_metadata_only() -> None:
    mbids = [f"00000000-0000-4000-8000-{index:012d}" for index in range(60)]

    class _BatchListenBrainz:
        hydrated_mbids = []

        def __init__(self, _server):
            pass

        def get(self, _url, *, params=None):
            return {
                "payload": {
                    "mbids": [
                        {"recording_mbid": mbid, "score": 1.0 - index / 100}
                        for index, mbid in enumerate(mbids)
                    ]
                }
            }

        def recording_metadata(self, values):
            self.__class__.hydrated_mbids = list(values)
            return {
                mbid: {
                    "title": f"Recommended {index}",
                    "artist": f"Artist {index}",
                    "duration": 200,
                }
                for index, mbid in enumerate(self.hydrated_mbids)
            }

    class _NoMusicBrainzServer:
        class _MusicBrainz:
            def search_recordings(self, *args, **kwargs):
                raise AssertionError("personal recommendations must use batch metadata")

        musicbrainz_client = _MusicBrainz()

    request = EnrichmentRequest(
        kind="listenbrainz_user_recommendations",
        key="listener",
        pool="collaborative",
        relation="collaborative_neighbor",
        limit=60,
    )
    with patch.object(enrichment_module, "ListenBrainzClient", _BatchListenBrainz):
        rows = enrichment_module._fetch_request(_NoMusicBrainzServer(), request)

    assert _BatchListenBrainz.hydrated_mbids == mbids
    assert len(rows) == 60
    assert [row["musicbrainz_recording_id"] for row in rows] == mbids
    assert rows[0]["relationship_score"] == 1.0


def test_release_year_completion_batches_and_persists_by_recording(tmp_path) -> None:
    recording_id = "11111111-1111-4111-8111-111111111111"
    calls = []

    class _MusicBrainz:
        def lookup_recordings(self, recording_ids, *, limit=40):
            calls.append(list(recording_ids))
            return [
                {
                    "id": recording_id,
                    "title": "Canonical song",
                    "artist-credit": [
                        {
                            "name": "Canonical artist",
                            "artist": {
                                "id": "22222222-2222-4222-8222-222222222222",
                                "name": "Canonical artist",
                            },
                        }
                    ],
                    "releases": [
                        {
                            "id": "33333333-3333-4333-8333-333333333333",
                            "title": "Original album",
                            "date": "1985-03-01",
                            "country": "GB",
                            "release-group": {
                                "id": "44444444-4444-4444-8444-444444444444"
                            },
                        }
                    ],
                }
            ]

    server = SimpleNamespace(
        RECOMMENDATION_STORE_DB_PATH=str(tmp_path / "recommendation.sqlite3"),
        recommendation_store_lock=threading.RLock(),
        musicbrainz_client=_MusicBrainz(),
    )
    candidate = DiscoveryCandidate(
        item={
            **_track(1, relation="track_radio"),
            "musicbrainz_recording_id": recording_id,
            "release_year": "",
            "release_date": "",
        },
        source="similarity",
        score=4.0,
        reasons=["track_radio"],
    )
    inventory = CandidateInventory(
        user_scope_id="release-year-test",
        profile_fingerprint="profile-a",
        generated_at=time.time(),
        expires_at=time.time() + 3600,
        pools={"similarity": [candidate]},
        generation_id="release-year-inventory",
    )

    completed = complete_inventory_release_metadata(server, inventory)
    assert calls == [[recording_id]]
    assert completed.pools["similarity"][0].item["release_year"] == "1985"
    assert completed.pools["similarity"][0].item["release_date"] == "1985-03-01"

    class _NoNetworkMusicBrainz:
        def lookup_recordings(self, *_args, **_kwargs):
            raise AssertionError("persisted metadata must avoid another lookup")

    server.musicbrainz_client = _NoNetworkMusicBrainz()
    cached = complete_inventory_release_metadata(server, inventory)
    assert cached.pools["similarity"][0].item["release_year"] == "1985"
    assert cached.candidate_counts["release_metadata_cache_hits"] == 1


def test_persisted_release_year_hydrates_artifact_and_changes_signature(tmp_path) -> None:
    recording_id = "11111111-1111-4111-8111-111111111111"

    class _MusicBrainz:
        def lookup_recordings(self, recording_ids, *, limit=40):
            return [
                {
                    "id": value,
                    "title": "Canonical song",
                    "artist-credit": [{"name": "Canonical artist"}],
                    "releases": [{"title": "Original album", "date": "1991-02-18"}],
                }
                for value in recording_ids
            ]

    server = SimpleNamespace(
        RECOMMENDATION_STORE_DB_PATH=str(tmp_path / "recommendation.sqlite3"),
        recommendation_store_lock=threading.RLock(),
        musicbrainz_client=_MusicBrainz(),
    )
    candidate = DiscoveryCandidate(
        item={**_track(7, relation="track_radio"), "musicbrainz_recording_id": recording_id},
        source="similarity",
        reasons=["track_radio"],
    )
    inventory = CandidateInventory(
        user_scope_id="artifact-release-year-test",
        profile_fingerprint="profile-a",
        generated_at=time.time(),
        expires_at=time.time() + 3600,
        pools={"discovery_universe": [candidate]},
        generation_id="artifact-release-year-inventory",
    )
    complete_inventory_release_metadata(server, inventory)
    artifact = _feed_artifact("artifact-release-year-test", "metadata-session", "track-a")
    artifact.rows[0].items[0]["musicbrainz_recording_id"] = recording_id

    service = DiscoveryService(server)
    before = service._artifact_signature(artifact)
    hydrated, pending = hydrate_artifact_release_metadata(server, artifact)
    after = service._artifact_signature(hydrated)
    service._prepare_executor.shutdown(wait=False, cancel_futures=True)

    assert pending == 0
    assert hydrated.rows[0].items[0]["release_year"] == "1991"
    assert before != after


def test_release_year_recovers_recording_id_from_playback_source(tmp_path) -> None:
    recording_id = "22222222-2222-4222-8222-222222222222"
    video_id = "abc123DEF45"

    class _MusicBrainz:
        def lookup_recordings(self, recording_ids, *, limit=40):
            assert recording_ids == [recording_id]
            return [
                {
                    "id": recording_id,
                    "title": "Mapped song",
                    "artist-credit": [{"name": "Mapped artist"}],
                    "releases": [{"title": "Mapped album", "date": "2004-06-01"}],
                }
            ]

    server = SimpleNamespace(
        RECOMMENDATION_STORE_DB_PATH=str(tmp_path / "recommendation.sqlite3"),
        recommendation_store_lock=threading.RLock(),
        musicbrainz_client=_MusicBrainz(),
    )
    from auralis_backend.recommend.store_runtime import (
        open_recommendation_store_connection,
    )

    connection = open_recommendation_store_connection(server)
    connection.execute(
        """
        INSERT INTO catalog_entity_sources(
            entity_type, entity_key, source_provider, source_key,
            source_authority, confidence, payload_json, updated_at
        ) VALUES ('track', ?, 'youtube', ?, 'topic', 1.0, '{}', ?)
        """,
        [f"musicbrainz:recording:{recording_id}", video_id, time.time()],
    )
    connection.commit()
    connection.close()

    artifact = _feed_artifact("source-year-test", "source-year-session", video_id)
    artifact.rows[0].items[0].pop("musicbrainz_recording_id", None)
    artifact.rows[0].items[0]["videoId"] = video_id
    artifact.rows[0].items[0]["playback"] = {
        "provider": "youtube",
        "source_id": video_id,
    }

    hydrated, pending = hydrate_artifact_release_metadata(server, artifact)

    assert pending == 0
    assert hydrated.rows[0].items[0]["musicbrainz_recording_id"] == recording_id
    assert hydrated.rows[0].items[0]["release_year"] == "2004"


def test_refresh_promotes_prepared_before_taste_reconciliation() -> None:
    user_scope = f"fast-refresh-{int(time.time() * 1000000)}"
    active = _feed_artifact(user_scope, "active-session", "active-track")
    prepared = _feed_artifact(user_scope, "prepared-session", "prepared-track")
    state = FeedState(
        user_scope_id=user_scope,
        active_feed=active,
        prepared_feed=prepared,
        active_version=3,
        prepared_base_version=3,
        profile_fingerprint="profile-a",
        generation_status="prepared",
    )
    reconcile_started = threading.Event()
    release_reconcile = threading.Event()

    def slow_taste(_server, _req):
        reconcile_started.set()
        release_reconcile.wait(timeout=2)
        taste = _taste()
        taste.user_scope_id = user_scope
        return taste

    service = DiscoveryService(object())
    try:
        assert save_feed_state(None, state) is True
        with patch(
            "auralis_backend.discovery.service.build_taste_profile",
            side_effect=slow_taste,
        ), patch.object(service, "_schedule_preparation"):
            response = service.recommend(
                SimpleNamespace(
                    user_scope_id=user_scope,
                    force_refresh=True,
                    refresh_token="refresh-test",
                ),
                request_mode="full_feed",
            )
            assert response["feed_action"] == "promoted_prepared"
            assert response["feed_version"] == 4
            assert reconcile_started.wait(timeout=1)
    finally:
        release_reconcile.set()
        service._prepare_executor.shutdown(wait=True, cancel_futures=True)
        invalidate_feed_state(None, user_scope)


def test_quiet_pick_reserve_is_large_and_non_blocking() -> None:
    taste = _taste()
    candidates = [
        DiscoveryCandidate(
            item=_track(index + 1000, relation="track_radio"),
            source="similarity",
            score=4.0,
            reasons=["track_radio"],
        )
        for index in range(180)
    ]
    inventory = CandidateInventory(
        user_scope_id=taste.user_scope_id,
        profile_fingerprint=taste.profile_key,
        generated_at=time.time(),
        expires_at=time.time() + 3600,
        pools={
            "similarity": candidates,
                "popularity": [
                    DiscoveryCandidate(
                        item={
                            **_track(index + 5000, relation="broad_global"),
                            "artist": f"Unrelated artist {index}",
                            "channel": f"Unrelated artist {index} - Topic",
                            "album": f"Unrelated album {index}",
                            "genre": "unrelated opera",
                            "language": "unknown",
                            "language_confidence": 0.0,
                    },
                    source="popularity",
                    score=20.0,
                    reasons=["broad_global"],
                )
                for index in range(30)
            ],
        },
        generation_id="reserve-inventory",
    )

    refreshed = refresh_candidate_inventory_coverage(inventory, taste=taste)
    assert ROW_RECIPES["quiet_picks"].max_items == 200
    assert refreshed.acquisition_ledger["row_reserve_targets"]["quiet_picks"] == 160
    assert refreshed.acquisition_ledger["row_reserve_shortages"]["quiet_picks"] == 0
    assert "quiet_picks" not in refreshed.coverage["failed_contracts"]
    ranked = rank_tracks(
        refreshed.pools,
        taste,
        ROW_RECIPES["quiet_picks"],
        limit=ROW_RECIPES["quiet_picks"].max_items,
    )
    assert len(ranked) == 180
    assert all(item["recommendation_path"] == "track_radio" for item in ranked)

    artifact = DiscoveryArtifact(
        session_id="quiet-paging-session",
        user_scope_id=taste.user_scope_id,
        profile_key=taste.profile_key,
        generated_at=time.time(),
        expires_at=time.time() + 3600,
        rows=[
            DiscoveryRow(
                id="quiet_picks",
                title="Quiet Picks",
                kind="quiet_picks",
                item_type="track",
                items=ranked,
                meta={
                    "page_size": 20,
                    "prepared_count": len(ranked),
                    "reserve_count": len(ranked) - 20,
                },
                next_offset=20,
                has_more=True,
            )
        ],
        diagnostics={},
        candidate_pool_counts={},
        provider_timings_ms={},
        home_tab_lanes={},
        accepted=True,
    )
    first = row_page_response_from_artifact(
        artifact,
        row_id="quiet_picks",
        offset=0,
        limit=20,
        request_id="page-1",
    )
    middle = row_page_response_from_artifact(
        artifact,
        row_id="quiet_picks",
        offset=20,
        limit=40,
        request_id="page-2",
    )
    final = row_page_response_from_artifact(
        artifact,
        row_id="quiet_picks",
        offset=160,
        limit=40,
        request_id="page-final",
    )
    assert first is not None and len(first["row"]["items"]) == 20
    assert middle is not None and len(middle["row"]["items"]) == 40
    assert final is not None and len(final["row"]["items"]) == 20
    assert final["row"]["has_more"] is False
    visible_ids = [
        item["track_key"]
        for page in (first, middle, final)
        for item in page["row"]["items"]
    ]
    assert len(visible_ids) == len(set(visible_ids))
