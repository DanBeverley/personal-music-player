from __future__ import annotations

from collections import Counter
from dataclasses import replace

from server import normalize_recommendation_track as production_normalize_track

from auralis_backend.discovery import enrichment as enrichment_module
from auralis_backend.discovery.enrichment import (
    CandidateEnrichmentPlan,
    EnrichmentRequest,
    MaterializedCandidateSupply,
    build_enrichment_plan,
    canonicalize_materialized_pools,
    materialize_enrichment_plan,
)
from auralis_backend.discovery.inventory import (
    build_candidate_inventory,
    candidate_inventory_coverage,
    canonicalize_candidate_pools,
)
from auralis_backend.discovery.radio_inventory import (
    build_artist_radio_inventory,
    radio_card_candidates,
)
from auralis_backend.discovery.artifact import evaluate_quality
from auralis_backend.discovery.config import ROW_ORDER
from auralis_backend.discovery.ranking import build_rows_from_pools
from auralis_backend.discovery.feed_state import _state_from_payload
from auralis_backend.discovery.schema import DiscoveryCandidate, TasteProfile
from auralis_backend.search.musicbrainz import browse_musicbrainz_artist_album_items


class _MaterializationServer:
    UPSTREAM_RETRY_ATTEMPTS = 1
    UPSTREAM_RETRY_BACKOFF_SECONDS = 0.0

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.musicbrainz_client = _MusicBrainzFixtureClient(self.calls)
        self.ytmusic = _YTMusicAlbumFixture(self.calls)

    @staticmethod
    def _recommendation_trim_text(value) -> str:
        return str(value or "").strip()

    @staticmethod
    def normalize_recommendation_track(raw):
        return production_normalize_track(raw)

    @staticmethod
    def _normalize_text(value) -> str:
        return " ".join(str(value or "").strip().casefold().split())

    def _search_upstream_call_with_retry(
        self,
        callback,
        *,
        attempts=1,
        backoff_seconds=0.0,
        default=None,
    ):
        try:
            return callback()
        except Exception:
            return default

    @staticmethod
    def normalize_album_results(results):
        return [dict(item) for item in results or [] if isinstance(item, dict)]

    def _assistant_tool_get_similar_tracks(self, anchor_id: str, limit: int):
        self.calls["similar"] += 1
        return [
            {
                "id": f"sim-{anchor_id}-{index}",
                "title": f"Adjacent track {anchor_id} {index}",
                "artist": f"Adjacent artist {anchor_id} {index}",
                "provider": "ytmusic",
                "album": f"Adjacent album {anchor_id}",
                "album_id": f"album-{anchor_id}",
                "genre": "rock",
                "language": "english",
                "language_confidence": 0.95,
            }
            for index in range(limit)
        ]

    def _assistant_tool_search_tracks(self, query: str, limit: int):
        self.calls["search"] += 1
        query_key = "-".join(query.casefold().split())
        generic_queries = {
            "rock songs",
            "chill mellow acoustic soul songs",
            "workout upbeat rock dance songs",
            "focus instrumental ambient study songs",
            "emotional atmospheric cinematic songs",
        }
        artist_seed = query
        for suffix in (" official audio", " top songs", " songs"):
            if artist_seed.casefold().endswith(suffix):
                artist_seed = artist_seed[: -len(suffix)].strip()
                break
        return [
            {
                "id": f"search-{query_key}-{index}",
                "title": f"Catalog track {query} {index}",
                "artist": (
                    f"Backbone artist {query_key} {index % 12}"
                    if query.casefold() in generic_queries
                    else artist_seed
                ),
                "provider": "ytmusic",
                "genre": "rock",
                "language": "english",
                "language_confidence": 0.95,
                "album": f"Catalog album {query_key} {index // 2}",
                "album_id": f"album-{query_key}-{index // 2}",
            }
            for index in range(limit)
        ]

    def _assistant_tool_get_album_details(self, album_id: str):
        self.calls["album_details"] += 1
        return {
            "id": album_id,
            "thumbnail": f"https://example.test/{album_id}.jpg",
            "tracks": [
                {
                    "id": f"{album_id}-track-{index}",
                    "title": f"Track {index}",
                    "artist": "Fixture artist",
                }
                for index in range(10)
            ],
        }


class _MusicBrainzFixtureClient:
    def __init__(self, calls: Counter[str]) -> None:
        self.calls = calls
        self.artist_names = {}

    def search_artists(self, query: str, *, limit: int = 5):
        self.calls["musicbrainz_artist"] += 1
        key = "-".join(query.casefold().split())
        artist_id = f"mb-artist-{key}"
        self.artist_names[artist_id] = query
        return [{"id": artist_id, "name": query, "score": 100}]

    def browse_artist_release_groups(
        self,
        artist_id: str,
        *,
        limit: int = 12,
        offset: int = 0,
    ):
        self.calls["musicbrainz_release_groups"] += 1
        artist = self.artist_names.get(artist_id, artist_id)
        return [
            {
                "id": f"mb-release-group-{artist_id}-{index + offset}",
                "title": f"Album {index + offset}",
                "primary-type": "Album",
                "first-release-date": f"{2000 + index}-01-01",
                "artist-credit": [
                    {"name": artist, "artist": {"id": artist_id, "name": artist}}
                ],
            }
            for index in range(limit)
        ]


class _YTMusicAlbumFixture:
    def __init__(self, calls: Counter[str]) -> None:
        self.calls = calls

    def search(self, query: str, *, filter=None, limit: int = 8):
        self.calls["ytmusic_album_resolution"] += 1
        marker = query.casefold().rfind(" album ")
        artist = query[:marker].strip() if marker >= 0 else query.strip()
        title = query[marker + 1 :].strip() if marker >= 0 else query.strip()
        key = "-".join(f"{artist}-{title}".casefold().split())
        return [
            {
                "id": f"yt-album-{key}",
                "browseId": f"yt-album-{key}",
                "title": title,
                "artist": artist,
                "provider": "ytmusic",
                "source_authority": "official",
            }
        ]


def _established_taste() -> TasteProfile:
    anchors = [
        {
            "id": f"history-{index}",
            "title": f"Played track {index}",
            "artist": f"Played artist {index}",
            "provider": "ytmusic",
            "source_authority": "verified_catalog",
            "genre": "rock",
        }
        for index in range(8)
    ]
    frequent = [
        {**track, "play_count": 20 - index, "last_played_at": 1000 - index}
        for index, track in enumerate(anchors)
    ]
    return TasteProfile(
        user_scope_id="coverage-test-user",
        profile_key="coverage-test-profile",
        signal_tier="established",
        recent_tracks=list(anchors),
        top_tracks=list(anchors),
        anchor_tracks=list(anchors),
        last_played_tracks=list(anchors),
        full_history_tracks=list(frequent),
        frequent_tracks=list(frequent),
        artist_hints=[track["artist"] for track in anchors],
        top_artists=[track["artist"] for track in anchors],
        taste_queries=["classic rock", "hard rock"],
        source_profile={
            "top_genres": ["rock", "blues rock"],
            "accepted_languages": ["english"],
        },
    )


def _attach_radio_inventory(inventory, taste):
    radio = build_artist_radio_inventory(taste, inventory.pools)
    pools = {name: list(values or []) for name, values in inventory.pools.items()}
    pools["popular_radio_cards"] = radio_card_candidates(radio)
    coverage = candidate_inventory_coverage(pools, taste=taste)
    counts = dict(inventory.candidate_counts or {})
    counts["popular_radio_cards"] = len(radio.cards)
    counts["coverage_ready"] = 1 if coverage.get("ready") is True else 0
    return replace(inventory, pools=pools, coverage=coverage, candidate_counts=counts), radio


def test_materialized_inventory_clears_established_candidate_shortage() -> None:
    server = _MaterializationServer()
    taste = _established_taste()
    plan = build_enrichment_plan(taste)
    supply = materialize_enrichment_plan(
        server,
        plan,
        time_budget_seconds=5.0,
        max_workers=8,
    )
    calls_after_materialization = server.calls.copy()

    inventory = build_candidate_inventory(
        server,
        taste,
        materialized_supply=supply,
    )

    inventory, radio = _attach_radio_inventory(inventory, taste)
    assert inventory.is_ready, inventory.coverage
    assert 8 <= len(radio.cards) <= 12
    assert all(len(card["tracks"]) == 24 for card in radio.cards)
    assert sum(card["seed_affinity"] == "direct" for card in radio.cards) / len(radio.cards) >= 0.60
    visible_radio_ids = [
        track["canonical_entity_id"]
        for card in radio.cards
        for track in card["tracks"][:8]
    ]
    assert len(visible_radio_ids) == len(set(visible_radio_ids))
    assert inventory.coverage["actual"]["unique_tracks"] >= 120
    assert inventory.coverage["actual"]["unplayed_tracks"] >= 48
    assert inventory.candidate_counts["coverage_ready"] == 1
    assert server.calls == calls_after_materialization, "inventory build performed a second live fetch"
    assert server.calls["musicbrainz_release_groups"] >= 8
    assert server.calls["ytmusic_album_resolution"] >= 20
    assert server.calls["album_details"] >= 20
    assert inventory.pools["album"]
    assert all(
        candidate.item.get("album_source") == "artist_catalog"
        and str(candidate.item.get("id") or "").startswith("yt-album-")
        and candidate.item.get("musicbrainz_release_group_id")
        and candidate.item.get("track_count", 0) >= 1
        for candidate in inventory.pools["album"]
    )

    history_ids = {track["id"] for track in taste.recent_tracks}
    discovery_ids = {
        candidate.item.get("id")
        for candidate in inventory.pools["discovery_universe"]
    }
    assert history_ids.isdisjoint(discovery_ids)


def test_canonical_inventory_rejects_metadata_only_and_prefers_official_upload() -> None:
    metadata_only = DiscoveryCandidate(
        item={
            "id": "musicbrainz:recording-a",
            "musicbrainz_recording_id": "recording-a",
            "title": "Song A",
            "artist": "Artist A",
            "provider": "musicbrainz",
        },
        source="similarity",
    )
    unofficial = DiscoveryCandidate(
        item={
            "id": "upload-unofficial",
            "musicbrainz_recording_id": "recording-b",
            "title": "Song B",
            "artist": "Artist B",
            "provider": "ytmusic",
        },
        source="similarity",
    )
    official = DiscoveryCandidate(
        item={
            "id": "upload-official",
            "musicbrainz_recording_id": "recording-b",
            "title": "Song B",
            "artist": "Artist B",
            "provider": "ytmusic",
            "source_authority": "official",
        },
        source="profile_spine",
    )

    pools, stats = canonicalize_candidate_pools(
        {"similarity": [metadata_only, unofficial], "profile_spine": [official]}
    )

    assert stats == {
        "raw_track_count": 3,
        "canonical_unique_track_count": 1,
        "duplicate_track_count": 1,
        "unplayable_track_count": 1,
    }
    assert pools["similarity"][0].item["id"] == "upload-official"
    assert pools["profile_spine"][0].item["id"] == "upload-official"


def test_materialized_supply_upgrades_semantic_duplicate_to_recording_mbid() -> None:
    recording_mbid = "135214af-6058-4705-9f17-18d396f36191"
    pools, stats = canonicalize_materialized_pools(
        {
            "similarity": [
                {
                    "title": "Hells Bells",
                    "artist": "AC/DC",
                    "relationship_score": 0.82,
                }
            ],
            "artist_graph": [
                {
                    "title": "Hells Bells",
                    "artist": "AC/DC",
                    "musicbrainz_recording_id": recording_mbid,
                    "relationship_score": 0.91,
                }
            ],
        }
    )

    expected_identity = f"musicbrainz:recording:{recording_mbid}"
    assert pools["similarity"][0]["canonical_entity_id"] == expected_identity
    assert pools["artist_graph"][0]["canonical_entity_id"] == expected_identity
    assert pools["similarity"][0]["track_key"] == f"recording:{recording_mbid}"
    assert stats == {
        "canonical_supply_raw_tracks": 2,
        "canonical_supply_unique_tracks": 1,
        "canonical_supply_duplicates_removed": 1,
        "canonical_supply_identity_upgrades": 1,
    }


def test_full_history_anchor_cursor_progresses_without_losing_diversity() -> None:
    taste = _established_taste()
    taste.full_history_tracks = [
        {
            "id": f"full-history-{index}",
            "title": f"History {index}",
            "artist": f"History artist {index}",
            "album": f"History album {index}",
            "play_count": 100 - index,
            "last_played_at": 1000 - index,
        }
        for index in range(48)
    ]
    first = build_enrichment_plan(taste)
    second = build_enrichment_plan(
        taste,
        acquisition_ledger={"anchor_cursor": first.anchor_cursor_next, "cycle": 1},
    )
    first_radio_keys = [request.key for request in first.requests if request.kind == "track_radio"]
    second_radio_keys = [request.key for request in second.requests if request.kind == "track_radio"]

    assert len(first_radio_keys) == 24
    assert len(second_radio_keys) == 24
    assert first.anchor_cursor_next != second.anchor_cursor_next
    assert set(second_radio_keys) - set(first_radio_keys)


def test_repeated_provider_page_is_persisted_and_marked_exhausted(monkeypatch) -> None:
    server = _MaterializationServer()
    monkeypatch.setattr(
        enrichment_module,
        "_fetch_request",
        lambda _server, _request: [
            {
                "title": f"Repeated track {index}",
                "artist": f"Repeated artist {index}",
                "musicbrainz_recording_id": f"00000000-0000-4000-8000-{index:012d}",
            }
            for index in range(24)
        ],
    )
    request = EnrichmentRequest(
        kind="lastfm_track_similar",
        key="same-anchor",
        pool="similarity",
        relation="track_radio",
        limit=24,
    )
    first = materialize_enrichment_plan(
        server,
        CandidateEnrichmentPlan(user_scope_id="repeat-user", requests=[request]),
        max_workers=1,
    )
    second = materialize_enrichment_plan(
        server,
        CandidateEnrichmentPlan(
            user_scope_id="repeat-user",
            requests=[request],
            prior_request_progress=first.diagnostics["request_progress"],
        ),
        max_workers=1,
    )
    third = materialize_enrichment_plan(
        server,
        CandidateEnrichmentPlan(
            user_scope_id="repeat-user",
            requests=[request],
            prior_request_progress=second.diagnostics["request_progress"],
        ),
        max_workers=1,
    )
    progress = next(iter(third.diagnostics["request_progress"].values()))

    assert len(first.pools["similarity"]) == 24
    assert second.pools["similarity"] == []
    assert third.pools["similarity"] == []
    assert progress["exhausted"] is True
    assert progress["cursor"] == 72


def test_materialization_saves_and_advances_small_job_groups(monkeypatch) -> None:
    jobs = {}
    fetched = []

    def load_job(_server, _scope, request):
        return jobs.get(request.key)

    def store_job(_server, _scope, request, payload):
        jobs[request.key] = {**payload, "expires_at": 10**12}

    def fetch_request(_server, request):
        fetched.append(request.key)
        return [
            {
                "track_key": f"recording:{request.key}",
                "title": f"Track {request.key}",
                "artist": "Test artist",
            }
        ]

    monkeypatch.setattr(enrichment_module, "_load_job", load_job)
    monkeypatch.setattr(enrichment_module, "_store_job", store_job)
    monkeypatch.setattr(enrichment_module, "_fetch_request", fetch_request)
    plan = CandidateEnrichmentPlan(
        user_scope_id="bounded-user",
        requests=[
            EnrichmentRequest(
                kind="fixture",
                key=str(index),
                pool="similarity",
                relation="fixture",
                limit=1,
            )
            for index in range(5)
        ],
    )

    first = materialize_enrichment_plan(
        object(), plan, max_workers=2, max_pending_jobs=2
    )
    second = materialize_enrichment_plan(
        object(), plan, max_workers=2, max_pending_jobs=2
    )
    third = materialize_enrichment_plan(
        object(), plan, max_workers=2, max_pending_jobs=2
    )

    assert first.diagnostics["scheduled_request_count"] == 2
    assert first.diagnostics["deferred_request_count"] == 3
    assert second.diagnostics["cached_request_count"] == 2
    assert second.diagnostics["deferred_request_count"] == 1
    assert third.diagnostics["cached_request_count"] == 4
    assert third.diagnostics["deferred_request_count"] == 0
    assert fetched == ["0", "1", "2", "3", "4"]
    assert len(third.pools["similarity"]) == 5


def test_structured_title_artist_does_not_call_musicbrainz_per_result() -> None:
    class _FailingMusicBrainzClient:
        def search_recordings(self, *args, **kwargs):
            raise AssertionError("structured title/artist must not trigger a MusicBrainz lookup")

    class _Server:
        musicbrainz_client = _FailingMusicBrainzClient()

    request = EnrichmentRequest(
        kind="lastfm_track_similar",
        key="anchor",
        pool="similarity",
        relation="track_radio",
        limit=4,
    )
    rows = enrichment_module._canonical_recording_rows(
        _Server(),
        [{"title": "The Chain", "artist": "Fleetwood Mac"}],
        request=request,
    )

    assert len(rows) == 1
    assert rows[0]["track_key"] == "recording:the chain|fleetwood mac"
    assert rows[0]["source_provenance"] == "structured:lastfm_track_similar"


def test_album_shortage_schedules_only_album_acquisition() -> None:
    plan = build_enrichment_plan(
        _established_taste(),
        acquisition_ledger={
            "failed_domains": ["featured_new_albums", "recommended_albums"],
        },
    )

    assert plan.requests
    assert {request.kind for request in plan.requests} == {"canonical_album_catalog"}
    assert {request.pool for request in plan.requests} == {"album"}


def test_album_replenishment_advances_musicbrainz_release_page() -> None:
    plan = build_enrichment_plan(
        _established_taste(),
        acquisition_ledger={
            "failed_domains": ["recommended_albums"],
            "request_progress": {
                "canonical_album_catalog:album:played artist 0": {
                    "cursor": 4,
                    "returned_identities": ["first-page-album"],
                }
            },
        },
    )

    request = next(
        request
        for request in plan.requests
        if request.kind == "canonical_album_catalog" and request.key == "Played artist 0"
    )
    assert request.metadata["release_group_offset"] == 4


def test_album_replenishment_uses_the_furthest_saved_release_page() -> None:
    plan = build_enrichment_plan(
        _established_taste(),
        acquisition_ledger={
            "failed_domains": ["recommended_albums"],
            "request_progress": {
                "canonical_album_catalog:album:played artist 0:0": {"cursor": 4},
                "canonical_album_catalog:album:played artist 0:4": {"cursor": 8},
            },
        },
    )

    request = next(
        request
        for request in plan.requests
        if request.kind == "canonical_album_catalog" and request.key == "Played artist 0"
    )
    assert request.metadata["release_group_offset"] == 8


def test_musicbrainz_artist_resolution_rejects_name_collision() -> None:
    class _CollisionClient:
        def search_artists(self, query: str, *, limit: int = 5):
            return [{"id": "wrong", "name": f"{query} Tribute", "score": 100}]

        def browse_artist_release_groups(self, artist_id: str, **kwargs):
            raise AssertionError("a non-exact artist must never be browsed")

    assert browse_musicbrainz_artist_album_items(
        "Queen",
        client=_CollisionClient(),
        limit=4,
    ) == []


def test_canonical_album_without_playable_tracks_is_not_materialized() -> None:
    class _EmptyAlbumServer(_MaterializationServer):
        def _assistant_tool_get_album_details(self, album_id: str):
            self.calls["album_details"] += 1
            return {"id": album_id, "tracks": []}

    server = _EmptyAlbumServer()
    plan = build_enrichment_plan(
        _established_taste(),
        acquisition_ledger={"failed_domains": ["recommended_albums"]},
    )
    supply = materialize_enrichment_plan(server, plan, max_workers=4)

    assert supply.pools.get("album", []) == []


def test_thin_candidate_supply_is_not_publishable() -> None:
    inventory = build_candidate_inventory(
        _MaterializationServer(),
        _established_taste(),
        materialized_supply=MaterializedCandidateSupply(),
    )

    assert not inventory.is_ready
    assert inventory.candidate_counts["coverage_ready"] == 0
    assert "todays_pick" in inventory.coverage["failed_contracts"]
    assert "popular_radio" in inventory.coverage["failed_contracts"]


def test_new_user_builds_complete_backbone_feed_without_history_rows() -> None:
    server = _MaterializationServer()
    taste = TasteProfile(
        user_scope_id="new-user",
        profile_key="new-user-profile",
        signal_tier="cold_start",
        source_profile={"top_genres": ["rock"]},
    )
    first_supply = materialize_enrichment_plan(
        server,
        build_enrichment_plan(taste),
        time_budget_seconds=5.0,
        max_workers=8,
    )
    first = build_candidate_inventory(server, taste, materialized_supply=first_supply)
    assert first.acquisition_ledger["backbone_artist_seeds"]
    second_supply = materialize_enrichment_plan(
        server,
        build_enrichment_plan(taste, acquisition_ledger=first.acquisition_ledger),
        time_budget_seconds=5.0,
        max_workers=8,
    )
    inventory = build_candidate_inventory(
        server,
        taste,
        previous=first,
        materialized_supply=second_supply,
    )
    inventory, radio = _attach_radio_inventory(inventory, taste)
    rows, _status, _lanes, diagnostics = build_rows_from_pools(inventory.pools, taste)
    accepted, reasons, _quality = evaluate_quality(
        rows=rows,
        taste=taste,
        home_tab_diagnostics=diagnostics,
    )
    row_kinds = {row.kind for row in rows}

    assert inventory.is_ready, inventory.coverage
    assert accepted, reasons
    assert len(radio.cards) >= 8
    assert "last_played" not in row_kinds
    assert "frequently_listened" not in row_kinds
    assert "because_you_played" not in row_kinds


def test_real_observed_pool_exposes_row_specific_shortages() -> None:
    taste = _established_taste()
    history = [
        DiscoveryCandidate(
            item={
                "id": f"played-{index}",
                "title": f"Played track {index}",
                "artist": f"Played artist {index}",
                "source_authority": "verified_catalog",
                "recommendation_path": "direct_history",
            },
            source="history",
            reasons=["direct_history"],
        )
        for index in range(170)
    ]
    taste.recent_tracks = [dict(candidate.item) for candidate in history]
    similar = [
        DiscoveryCandidate(
            item={
                "id": f"similar-{index}",
                "title": f"Related track {index}",
                "artist": f"Related artist {index}",
                "source_authority": "verified_catalog",
                "recommendation_path": "track_radio",
            },
            source="similarity",
            reasons=["track_radio"],
        )
        for index in range(10)
    ]
    discovery = [
        DiscoveryCandidate(
            item={
                "id": f"discovery-{index}",
                "title": f"Discovery track {index}",
                "artist": f"Discovery artist {index}",
                "source_authority": "verified_catalog",
                "recommendation_path": "lane_query",
                "genre": "rock",
                "language": "english",
                "language_confidence": 0.95,
            },
            source="lane_mood",
            reasons=["lane_query"],
        )
        for index in range(51)
    ]

    coverage = candidate_inventory_coverage(
        {"history": history, "similarity": similar, "lane_mood": discovery},
        taste=taste,
    )

    assert coverage["actual"]["unique_tracks"] == 231
    assert coverage["actual"]["unplayed_tracks"] == 61
    assert not coverage["ready"]
    assert "popular_radio" in coverage["failed_contracts"]
    assert "recommended_albums" in coverage["failed_contracts"]


def test_production_shaped_supply_builds_complete_unplayed_home_feed() -> None:
    server = _MaterializationServer()
    taste = _established_taste()
    taste.frequent_tracks = list(reversed(taste.frequent_tracks))
    supply = materialize_enrichment_plan(
        server,
        build_enrichment_plan(taste),
        time_budget_seconds=5.0,
        max_workers=8,
    )
    inventory = build_candidate_inventory(
        server,
        taste,
        materialized_supply=supply,
    )
    inventory, _radio = _attach_radio_inventory(inventory, taste)

    rows, _status, _lanes, diagnostics = build_rows_from_pools(
        inventory.pools,
        taste,
    )
    accepted, reasons, _quality = evaluate_quality(
        rows=rows,
        taste=taste,
        home_tab_diagnostics=diagnostics,
    )

    assert accepted, reasons
    assert [row.kind for row in rows] == ROW_ORDER
    frequent_row = next(row for row in rows if row.kind == "frequently_listened")
    assert [item["play_count"] for item in frequent_row.items] == sorted(
        [item["play_count"] for item in frequent_row.items],
        reverse=True,
    )
    assert all("last_played_at" in item for item in frequent_row.items)
    assert diagnostics["allocation"]["duplicate_track_count"] == 0
    assert diagnostics["allocation"]["discovery_ratio"] >= 0.6

    history_ids = {track["id"] for track in taste.recent_tracks}
    recommendation_ids = {
        item.get("id")
        for row in rows
        if row.item_type == "track" and row.kind not in {"last_played", "frequently_listened"}
        for item in row.items
    }
    assert history_ids.isdisjoint(recommendation_ids)
    assert all(
        item.get("source_authority") in {"official", "canonical", "verified_catalog"}
        for row in rows
        if row.item_type == "track" and row.kind not in {"last_played", "frequently_listened"}
        for item in row.items
    )


def test_legacy_thin_artifact_is_removed_during_version_cutover() -> None:
    state = _state_from_payload(
        {
            "state_version": "feed-state-v2",
            "user_scope_id": "coverage-test-user",
            "active_version": 4,
            "generation_status": "build_failed",
            "active_feed": {
                "artifact_version": "discovery_home_artifact_v5",
                "session_id": "legacy-thin-feed",
                "accepted": True,
                "rows": [{"kind": "quiet_picks", "items": [{"id": "old"}]}],
            },
        }
    )

    assert state is not None
    assert state.active_feed is None
    assert state.generation_status == "idle"
    assert state.dirty_reasons == ["artifact_version_cutover"]
