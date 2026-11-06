from __future__ import annotations

from dataclasses import replace
import json
import pathlib
import sqlite3
import sys
import time
from types import SimpleNamespace
import pytest


CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.discovery import enrichment  # noqa: E402
from auralis_backend.discovery.enrichment import (  # noqa: E402
    CandidateEnrichmentPlan,
    EnrichmentRequest,
    build_enrichment_plan,
    materialize_enrichment_plan,
)
from auralis_backend.discovery.inventory import (  # noqa: E402
    CandidateInventory,
    candidate_inventory_refresh_seed,
    candidate_inventory_to_payload,
)
from auralis_backend.discovery.radio_inventory import (  # noqa: E402
    RADIO_INVENTORY_NAMESPACE,
    RADIO_INVENTORY_VERSION,
    ArtistRadioInventory,
    artist_radio_inventory_from_payload,
    artist_radio_inventory_to_payload,
    merge_store_artist_radio_inventory,
)
from auralis_backend.discovery.schema import DiscoveryRow, TasteProfile  # noqa: E402
from auralis_backend.home_feed.bundles import (  # noqa: E402
    is_complete_feed_bundle_payload,
)
from auralis_backend.home_feed.composer import HomeFeedComposer  # noqa: E402
from auralis_backend.home_feed.contracts import (  # noqa: E402
    CANDIDATE_SNAPSHOT_TTL_SECONDS,
    RADIO_SNAPSHOT_TTL_SECONDS,
    CandidateSnapshotRecord,
    RadioSnapshotRecord,
    evaluate_feed_contract,
    evaluate_popular_radio_contract,
    home_supply_profile_fingerprint,
)
from auralis_backend.home_feed.repositories import HomeRepositorySet  # noqa: E402
from auralis_backend.home_feed.radio_supply import RadioSupplyService  # noqa: E402
from auralis_backend.home_feed.scheduler import HomeJobScheduler  # noqa: E402
from auralis_backend.search import musicbrainz  # noqa: E402
from auralis_backend.storage.artist_artwork import artist_artwork_token  # noqa: E402


BUILD_BUDGET_SECONDS = 5.0


def _factory(path: pathlib.Path):
    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    return connect


def test_musicbrainz_album_requests_use_slim_official_payloads(monkeypatch) -> None:
    from urllib.parse import parse_qs, urlparse

    urls: list[str] = []
    payloads = iter(({"release-groups": []}, {"releases": []}))

    class Response:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    def urlopen(request, *, timeout):
        assert timeout == 8.0
        urls.append(request.full_url)
        return Response(next(payloads))

    monkeypatch.setattr(musicbrainz.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(musicbrainz, "MUSICBRAINZ_MIN_INTERVAL_SECONDS", 0.0)
    client = musicbrainz.MusicBrainzClient(timeout_seconds=8.0)

    client.browse_artist_release_groups("artist-1", limit=1)
    client.browse_release_group_releases("release-group-1")

    artist_query = parse_qs(urlparse(urls[0]).query)
    release_query = parse_qs(urlparse(urls[1]).query)
    assert artist_query["inc"] == ["artist-credits"]
    assert "tags" not in artist_query["inc"][0]
    assert release_query["limit"] == ["1"]
    assert release_query["status"] == ["official"]
    assert release_query["inc"] == ["recordings+artist-credits"]
    assert "media" not in release_query["inc"][0]


def test_musicbrainz_failure_preserves_phase_and_timeout_evidence(monkeypatch) -> None:
    monkeypatch.setattr(musicbrainz, "MUSICBRAINZ_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(
        musicbrainz.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TimeoutError("provider did not respond")
        ),
    )

    with pytest.raises(musicbrainz.MusicBrainzRequestFailure) as captured:
        musicbrainz.MusicBrainzClient(timeout_seconds=8.0).browse_artist_release_groups(
            "artist-1",
            limit=1,
        )

    assert captured.value.path == "release-group"
    assert captured.value.cause_type == "TimeoutError"
    assert captured.value.transient is True


def _radio_card(index: int, *, discovery: bool = False) -> dict:
    identity = f"musicbrainz:artist:{index}"
    token = artist_artwork_token(identity)
    artwork = f"/artist_artwork/{token}"
    tracks = [{"id": f"radio-track:{index}:{track}"} for track in range(12)]
    return {
        "id": f"radio:{identity}",
        "seed_artist_identity": identity,
        "seed_artist_identity_token": token,
        "artwork_owner_identity": identity,
        "artwork_owner_token": token,
        "seed_affinity": "exploratory" if discovery else "direct",
        "radio_mode": "artist_inventory",
        "thumbnail": artwork,
        "collage_images": [artwork],
        "tracks": tracks,
        "items": tracks,
    }


def _radio_mix(index: int, left: dict, right: dict) -> dict:
    tracks = list(left["tracks"][:6]) + list(right["tracks"][:6])
    return {
        "id": f"radio:mix:{index}",
        "seed_artist_identity": f"mix:{index}",
        "seed_artist_identities": [
            left["seed_artist_identity"],
            right["seed_artist_identity"],
        ],
        "seed_affinity": "direct",
        "radio_mode": "multi_artist_mix",
        "thumbnail": left["thumbnail"],
        "collage_images": [left["thumbnail"], right["thumbnail"]],
        "tracks": tracks,
        "items": tracks,
    }


def _radio_inventory(*, visible: bool) -> ArtistRadioInventory:
    now = time.time()
    artists = [_radio_card(index, discovery=index < 4) for index in range(20)]
    reservoir = [
        *artists,
        _radio_mix(0, artists[0], artists[1]),
        _radio_mix(1, artists[2], artists[3]),
    ]
    selected = [reservoir[-2], reservoir[-1], *artists[:10]]
    return ArtistRadioInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=now,
        expires_at=now + RADIO_SNAPSHOT_TTL_SECONDS,
        generation_id="radio-generation-1",
        cards=list(selected) if visible else [],
        reservoir_cards=reservoir,
        diagnostics={"rotation_epoch": 0},
    )


def test_radio_profile_cutover_reuses_identical_semantic_snapshot(
    tmp_path: pathlib.Path,
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "radio-cutover.sqlite"))
    legacy = _radio_inventory(visible=True)
    repositories.radio.put(
        RadioSnapshotRecord(
            snapshot_id="radio-legacy",
            user_scope_id=legacy.user_scope_id,
            profile_fingerprint=legacy.profile_fingerprint,
            generated_at=legacy.generated_at,
            expires_at=legacy.expires_at,
            payload=artist_radio_inventory_to_payload(legacy),
        )
    )
    stable = replace(
        legacy,
        profile_fingerprint=home_supply_profile_fingerprint("user-1"),
    )

    snapshot_id = RadioSupplyService(
        object(), repositories
    )._publish_snapshot(stable)

    assert snapshot_id == "radio-legacy"
    assert repositories.radio.get("radio-legacy") is not None


def test_compose_jobs_coalesce_identical_content_across_snapshot_ids(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "semantic-compose.sqlite"))
    monkeypatch.setattr(
        HomeRepositorySet,
        "from_server",
        classmethod(lambda _cls, _server: repositories),
    )
    now = time.time()
    candidates = [
        CandidateInventory(
            user_scope_id="user-1",
            profile_fingerprint=f"profile-{index}",
            generated_at=now - index,
            expires_at=now - index + CANDIDATE_SNAPSHOT_TTL_SECONDS,
            generation_id=f"candidate-{index}",
            coverage={"ready": True},
        )
        for index in range(2)
    ]
    radio = _radio_inventory(visible=True)
    radios = [radio, replace(radio, profile_fingerprint="profile-1")]
    for index, candidate in enumerate(candidates):
        repositories.candidates.put(
            CandidateSnapshotRecord(
                snapshot_id=candidate.generation_id,
                user_scope_id=candidate.user_scope_id,
                profile_fingerprint=candidate.profile_fingerprint,
                generated_at=candidate.generated_at,
                expires_at=candidate.expires_at,
                payload=candidate_inventory_to_payload(candidate),
            )
        )
        repositories.radio.put(
            RadioSnapshotRecord(
                snapshot_id=f"radio-{index}",
                user_scope_id="user-1",
                profile_fingerprint=radios[index].profile_fingerprint,
                generated_at=now - index,
                expires_at=now - index + RADIO_SNAPSHOT_TTL_SECONDS,
                payload=artist_radio_inventory_to_payload(radios[index]),
            )
        )
    scheduler = HomeJobScheduler(object())

    first = scheduler.compose(
        {"user_scope_id": "user-1"},
        reason="legacy-profile",
        candidate_snapshot_id="candidate-0",
        radio_snapshot_id="radio-0",
    )
    second = scheduler.compose(
        {"user_scope_id": "user-1"},
        reason="stable-profile",
        candidate_snapshot_id="candidate-1",
        radio_snapshot_id="radio-1",
    )

    assert second.job_id == first.job_id


def test_populated_radio_reservoir_materializes_publishable_cards(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    connect = _factory(tmp_path / "radio-selection.sqlite")
    connection = connect()
    try:
        connection.execute(
            """
            CREATE TABLE recommendation_feature_store(
                namespace TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                model_id TEXT,
                payload_json TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(namespace, entity_id)
            )
            """
        )
        broken = _radio_inventory(visible=False)
        connection.execute(
            "INSERT INTO recommendation_feature_store VALUES (?, ?, ?, ?, ?)",
            [
                RADIO_INVENTORY_NAMESPACE,
                "artist-radio-inventory:user-1",
                RADIO_INVENTORY_VERSION,
                json.dumps(artist_radio_inventory_to_payload(broken)),
                time.time(),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr(
        "auralis_backend.discovery.radio_inventory.open_recommendation_store_connection",
        lambda _server: connect(),
    )

    assert merge_store_artist_radio_inventory(object(), broken) is True
    connection = connect()
    try:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM recommendation_feature_store"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    repaired = artist_radio_inventory_from_payload(payload)

    assert repaired is not None
    assert len(repaired.reservoir_cards) == 22
    assert 10 <= len(repaired.cards) <= 15
    assert repaired.is_ready is True
    assert evaluate_popular_radio_contract(repaired.cards).accepted is True


def test_radio_supply_publishes_a_full_reservoir_without_provider_work(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    candidate = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=time.time(),
        expires_at=time.time() + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        generation_id="candidate-generation-1",
        coverage={"ready": True},
    )
    broken = _radio_inventory(visible=False)
    selected: list[ArtistRadioInventory] = []

    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.load_candidate_inventory",
        lambda *_args, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.load_artist_radio_inventory",
        lambda *_args, **_kwargs: selected[-1] if selected else broken,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.merge_store_artist_radio_inventory",
        lambda _server, inventory: selected.append(inventory) or True,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.materialize_enrichment_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("provider acquisition must not run")
        ),
    )
    service = RadioSupplyService(
        object(), HomeRepositorySet(_factory(tmp_path / "radio-service.sqlite"))
    )
    monkeypatch.setattr(service, "_publish_snapshot", lambda _inventory: "radio-ready")

    outcome = service.refresh(
        TasteProfile(
            user_scope_id="user-1",
            profile_key="profile-1",
            signal_tier="established",
        )
    )

    assert outcome.ready is True
    assert outcome.snapshot_id == "radio-ready"
    assert outcome.scheduled_requests == 0
    assert outcome.reason == "ready_reservoir_reused"
    assert 10 <= len(selected[-1].cards) <= 15


def test_radio_snapshot_identity_changes_only_when_payload_changes(
    tmp_path: pathlib.Path,
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "radio-immutable.sqlite"))
    service = RadioSupplyService(object(), repositories)
    inventory = _radio_inventory(visible=True)

    first = service._publish_snapshot(inventory)
    repeated = service._publish_snapshot(inventory)
    metadata_only = service._publish_snapshot(
        replace(
            inventory,
            diagnostics={**dict(inventory.diagnostics or {}), "rotation_epoch": 1},
        )
    )
    changed_cards = [dict(card) for card in inventory.reservoir_cards]
    changed_cards[0] = {
        **changed_cards[0],
        "tracks": [
            *list(changed_cards[0].get("tracks") or []),
            {"id": "newly-verified-track"},
        ],
    }
    changed = service._publish_snapshot(
        replace(
            inventory,
            cards=changed_cards,
            reservoir_cards=changed_cards,
        )
    )

    assert repeated == first
    assert metadata_only == first
    assert changed != first
    assert repositories.radio.get(first) is not None
    assert repositories.radio.get(changed) is not None


def test_servable_radio_with_discovery_deficit_still_checks_expansion_supply(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    from types import SimpleNamespace
    from auralis_backend.discovery.schema import DiscoveryCandidate

    candidate = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=time.time(),
        expires_at=time.time() + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        generation_id="candidate-generation-1",
        pools={
            "artist_graph": [
                DiscoveryCandidate(
                    {
                        "id": "track:discovery",
                        "title": "Discovery",
                        "artist": "New Artist",
                        "provider_artist_id": "new-artist",
                    },
                    "artist_graph",
                    1.0,
                )
            ]
        },
        coverage={"ready": True},
    )
    existing = _radio_inventory(visible=True)
    planned: list[object] = []
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.load_candidate_inventory",
        lambda *_args, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.load_artist_radio_inventory",
        lambda *_args, **_kwargs: existing,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.build_enrichment_plan",
        lambda *_args, **_kwargs: planned.append(object()) or object(),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.materialize_enrichment_plan",
        lambda *_args, **_kwargs: SimpleNamespace(
            pools={}, diagnostics={"scheduled_request_count": 0}
        ),
    )
    service = RadioSupplyService(
        object(), HomeRepositorySet(_factory(tmp_path / "radio-expand.sqlite"))
    )

    outcome = service.refresh(
        TasteProfile(
            user_scope_id="user-1",
            profile_key="profile-1",
            signal_tier="established",
        )
    )

    assert planned
    assert outcome.ready is True
    assert outcome.reason == "ready_reservoir_reused_no_expansion_work"


def test_empty_album_page_exhausts_the_artist_across_future_pages(
    monkeypatch,
) -> None:
    stored: list[dict] = []
    monkeypatch.setattr(enrichment, "_load_job", lambda *_args: None)
    monkeypatch.setattr(
        enrichment,
        "_store_job",
        lambda _server, _scope, _request, progress: stored.append(dict(progress)),
    )
    monkeypatch.setattr(enrichment, "_fetch_request", lambda *_args: [])
    request = EnrichmentRequest(
        kind="album_catalog",
        provider="musicbrainz",
        key="Artist One",
        pool="album",
        relation="artist_album_catalog",
        limit=4,
        metadata={"profile_seed_artist": "Artist One", "release_group_offset": 0},
    )
    result = materialize_enrichment_plan(
        object(),
        CandidateEnrichmentPlan(user_scope_id="user-1", requests=[request]),
        time_budget_seconds=1,
        max_workers=1,
        max_pending_jobs=1,
    )
    progress = result.diagnostics["request_progress"]
    aggregate = progress["album_catalog:musicbrainz:album:artist one"]

    assert aggregate["state"] == "exhausted"
    assert aggregate["exhausted"] is True
    assert aggregate["last_raw_count"] == 0
    assert stored[-1]["exhausted"] is True

    next_plan = build_enrichment_plan(
        TasteProfile(
            user_scope_id="user-1",
            profile_key="profile-1",
            signal_tier="established",
            artist_hints=["Artist One"],
        ),
        acquisition_ledger={
            "request_progress": progress,
            "album_shelf_shortages": {"recommended_albums": 8},
        },
        allowed_pools={"album"},
    )
    assert not any(
        request.kind == "album_catalog"
        and request.provider == "musicbrainz"
        and request.key.casefold() == "artist one"
        for request in next_plan.requests
    )


def test_album_plan_pages_one_release_group_per_durable_job() -> None:
    plan = build_enrichment_plan(
        TasteProfile(
            user_scope_id="user-1",
            profile_key="profile-1",
            signal_tier="established",
            artist_hints=["Artist One", "Artist Two"],
            recent_tracks=[
                {
                    "title": "Known Track",
                    "artist": "Artist One",
                    "musicbrainz_artist_id": "artist-one-mbid",
                }
            ],
        ),
        acquisition_ledger={
            "album_shelf_shortages": {"recommended_albums": 8},
        },
        allowed_pools={"album"},
    )
    album_requests = [
        request
        for request in plan.requests
        if request.kind == "album_catalog"
        and request.provider == "musicbrainz"
    ]
    ytmusic_requests = [
        request
        for request in plan.requests
        if request.kind == "album_catalog"
        and request.provider == "ytmusic"
    ]

    assert album_requests
    assert all(request.limit == 1 for request in album_requests)
    assert ytmusic_requests


def test_artist_catalog_plan_uses_independent_provider_jobs() -> None:
    plan = build_enrichment_plan(
        TasteProfile(
            user_scope_id="user-1",
            profile_key="profile-1",
            signal_tier="established",
            artist_hints=["Artist One"],
            recent_tracks=[
                {
                    "title": "Known Track",
                    "artist": "Artist One",
                    "musicbrainz_artist_id": "artist-one-mbid",
                }
            ],
        ),
        allowed_pools={"profile_spine"},
    )

    providers = {
        request.provider
        for request in plan.requests
        if request.kind == "artist_catalog"
    }
    assert providers == {"ytmusic", "lastfm", "listenbrainz"}
    assert all(request.pool == "profile_spine" for request in plan.requests)


def test_musicbrainz_failure_does_not_discard_other_catalog_supply(
    monkeypatch,
) -> None:
    monkeypatch.setattr(enrichment, "_load_job", lambda *_args: None)
    monkeypatch.setattr(enrichment, "_store_job", lambda *_args: None)

    def provider(_server, request):
        if request.provider == "musicbrainz":
            raise TimeoutError("temporary canonical provider failure")
        return [
            {
                "id": "yt-catalog-1",
                "videoId": "yt-catalog-1",
                "title": "Available Track",
                "artist": "Artist One",
                "provider": "ytmusic",
            }
        ]

    monkeypatch.setattr(enrichment, "_fetch_request", provider)
    plan = CandidateEnrichmentPlan(
        user_scope_id="user-1",
        requests=[
            EnrichmentRequest(
                kind="artist_catalog",
                provider=provider_name,
                key="Artist One",
                pool="radio_artist_catalog",
                relation="same_artist_catalog",
                limit=12,
                metadata={"profile_seed_artist": "Artist One"},
            )
            for provider_name in ("ytmusic", "musicbrainz")
        ],
    )

    result = materialize_enrichment_plan(
        object(), plan, max_workers=2, max_pending_jobs=2
    )

    assert result.diagnostics["scheduled_provider_counts"] == {
        "ytmusic": 1,
        "musicbrainz": 1,
    }
    assert result.diagnostics["completed_request_count"] == 1
    assert result.diagnostics["failed_request_count"] == 1
    assert len(result.pools["radio_artist_catalog"]) == 1


def test_ytmusic_artist_catalog_keeps_only_the_requested_artist() -> None:
    class YtMusic:
        def search(self, *_args, **_kwargs):
            return [
                {
                    "videoId": "owned-track",
                    "title": "Owned Track",
                    "artists": [{"name": "Artist One", "id": "artist-one"}],
                    "album": {"name": "Owned Album", "id": "album-one"},
                },
                {
                    "videoId": "wrong-track",
                    "title": "Wrong Track",
                    "artists": [{"name": "Other Artist", "id": "other"}],
                },
            ]

    request = EnrichmentRequest(
        kind="artist_catalog",
        provider="ytmusic",
        key="Artist One",
        pool="radio_artist_catalog",
        relation="same_artist_catalog",
        limit=12,
        metadata={"profile_seed_artist": "Artist One"},
    )
    rows = enrichment._resolve_ytmusic_artist_catalog_rows(
        type("Server", (), {"ytmusic": YtMusic()})(),
        request,
    )

    assert [row["videoId"] for row in rows] == ["owned-track"]
    assert rows[0]["source_identity_authority"] == "verified_catalog"


def test_ytmusic_album_catalog_uses_provider_neutral_identity() -> None:
    class YtMusic:
        def search(self, *_args, **_kwargs):
            return [
                {
                    "browseId": "album-one",
                    "title": "Album One",
                    "artists": [{"name": "Artist One", "id": "artist-one"}],
                }
            ]

        def get_album(self, _browse_id):
            return {
                "year": "2026",
                "tracks": [
                    {
                        "videoId": f"track-{index}",
                        "title": f"Track {index}",
                        "artists": [{"name": "Artist One", "id": "artist-one"}],
                    }
                    for index in range(3)
                ],
            }

    request = EnrichmentRequest(
        kind="album_catalog",
        provider="ytmusic",
        key="Artist One",
        pool="album",
        relation="artist_album_catalog",
        limit=3,
        metadata={"profile_seed_artist": "Artist One"},
    )
    rows = enrichment._resolve_ytmusic_album_rows(
        type("Server", (), {"ytmusic": YtMusic()})(),
        request,
    )

    assert len(rows) == 1
    assert rows[0]["canonical_album_identity"] == "ytmusic:album:album-one"
    assert not rows[0].get("musicbrainz_release_group_id")
    assert len(rows[0]["canonical_tracks"]) == 3


def test_same_album_from_two_providers_merges_into_one_entity() -> None:
    pools, _diagnostics = enrichment.canonicalize_materialized_pools(
        {
            "album": [
                {
                    "id": "ytmusic:album:album-one",
                    "canonical_album_identity": "ytmusic:album:album-one",
                    "title": "Album One",
                    "artist": "Artist One",
                    "source_provenance": "structured:album_catalog:ytmusic",
                    "canonical_tracks": [
                        {"title": "Track One", "artist": "Artist One"}
                    ],
                },
                {
                    "id": "musicbrainz:release-group:rg-one",
                    "canonical_album_identity": "musicbrainz:release-group:rg-one",
                    "musicbrainz_release_group_id": "rg-one",
                    "title": "Album One",
                    "artist": "Artist One",
                    "source_provenance": "structured:album_catalog:musicbrainz",
                    "canonical_tracks": [
                        {"title": "Track Two", "artist": "Artist One"}
                    ],
                },
            ]
        }
    )

    assert len(pools["album"]) == 1
    merged = pools["album"][0]
    assert merged["musicbrainz_release_group_id"] == "rg-one"
    assert len(merged["canonical_tracks"]) == 2
    assert set(merged["source_provenances"]) == {
        "structured:album_catalog:ytmusic",
        "structured:album_catalog:musicbrainz",
    }


def test_materialization_limits_musicbrainz_to_one_job_per_batch(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(enrichment, "_load_job", lambda *_args: None)
    monkeypatch.setattr(enrichment, "_store_job", lambda *_args: None)

    def provider(_server, request):
        calls.append((request.kind, request.provider))
        return []

    monkeypatch.setattr(enrichment, "_fetch_request", provider)
    plan = CandidateEnrichmentPlan(
        user_scope_id="user-1",
        requests=[
            EnrichmentRequest(
                kind="album_catalog",
                provider="musicbrainz",
                key="artist-a",
                pool="album",
                relation="artist_album_catalog",
                limit=4,
                metadata={"profile_seed_artist": "Artist A"},
            ),
            EnrichmentRequest(
                kind="artist_catalog",
                provider="musicbrainz",
                key="artist-b",
                pool="radio_artist_catalog",
                relation="same_artist_catalog",
                limit=4,
                metadata={"profile_seed_artist": "Artist B"},
            ),
        ],
    )
    result = materialize_enrichment_plan(
        object(), plan, max_workers=4, max_pending_jobs=4
    )
    assert sum(
        provider == "musicbrainz"
        for _kind, provider in calls
    ) == 1
    assert result.diagnostics["scheduled_request_count"] == 1


def test_musicbrainz_lane_leaves_capacity_for_non_musicbrainz_work(monkeypatch) -> None:
    monkeypatch.setattr(enrichment, "_load_job", lambda *_args: None)
    monkeypatch.setattr(enrichment, "_store_job", lambda *_args: None)
    calls: list[str] = []

    def provider(_server, request):
        calls.append(request.kind)
        return [{"id": f"{request.kind}:{request.key}", "title": request.key, "artist": "A"}]

    monkeypatch.setattr(enrichment, "_fetch_request", provider)
    requests = [
        EnrichmentRequest(
            "album_catalog", f"mb-{i}", "album", "catalog", 1,
            provider="musicbrainz",
        )
        for i in range(6)
    ] + [
        EnrichmentRequest("lastfm_tag_tracks", "genre-0", "genre_mood", "tag", 1),
        EnrichmentRequest("lastfm_track_similar", "track-0", "similarity", "similar", 1),
        *[
            EnrichmentRequest("lastfm_tag_tracks", f"genre-{i}", "genre_mood", "tag", 1)
            for i in range(1, 4)
        ],
    ]
    result = materialize_enrichment_plan(
        object(), CandidateEnrichmentPlan(user_scope_id="u", requests=requests),
        max_workers=6, max_pending_jobs=6,
    )
    assert result.diagnostics["scheduled_request_count"] == 6
    assert sum(kind == "album_catalog" for kind in calls) == 1
    assert sum(kind == "lastfm_tag_tracks" for kind in calls) == 4
    assert "lastfm_track_similar" in calls


def test_shared_musicbrainz_backoff_suppresses_next_cycle(monkeypatch) -> None:
    monkeypatch.setattr(enrichment, "_load_job", lambda *_args: None)
    monkeypatch.setattr(enrichment, "_store_job", lambda *_args: None)
    monkeypatch.setattr(enrichment.time, "time", lambda: 100.0)
    request = EnrichmentRequest(
        "album_catalog", "artist-a", "album", "catalog", 1,
        provider="musicbrainz",
    )
    plan = CandidateEnrichmentPlan(
        user_scope_id="u",
        requests=[request],
        prior_request_progress={
            enrichment._request_progress_signature(request): {
                "state": "retryable", "provider": "musicbrainz", "provider_retry_at": 200.0,
            }
        },
    )
    result = materialize_enrichment_plan(object(), plan, max_workers=1, max_pending_jobs=6)
    assert result.diagnostics["scheduled_request_count"] == 0


def test_known_musicbrainz_artist_id_skips_artist_search(monkeypatch) -> None:
    from types import SimpleNamespace
    import auralis_backend.search.musicbrainz as mb

    class Client:
        def search_artists(self, *_args, **_kwargs):
            raise AssertionError("known MBID must not search artist")

    monkeypatch.setattr(mb, "browse_musicbrainz_artist_album_items", lambda *args, **kwargs: [
        {"musicbrainz_release_group_id": "rg-1", "title": "Album", "artist": "Artist"}
    ])
    monkeypatch.setattr(mb, "browse_musicbrainz_release_group_tracks", lambda *args, **kwargs: [
        {"musicbrainz_recording_id": "rec-1", "title": "Track", "artist": "Artist"}
    ])
    request = EnrichmentRequest(
        "album_catalog", "mbid-1", "album", "catalog", 1,
        metadata={"profile_seed_artist": "Artist", "musicbrainz_artist_id": "known-mbid"},
        provider="musicbrainz",
    )
    rows = enrichment._resolve_musicbrainz_album_rows(
        SimpleNamespace(musicbrainz_client=Client()), request
    )
    assert rows and rows[0]["musicbrainz_artist_id"] == "known-mbid"


def test_musicbrainz_success_uses_long_ttl_but_failure_does_not(monkeypatch) -> None:
    class Connection:
        def __init__(self):
            self.values = []
        def execute(self, _sql, params):
            self.values.append(params)
        def commit(self):
            pass
        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(
        "auralis_backend.recommend.store_runtime.open_recommendation_store_connection",
        lambda _server: connection,
    )
    request = EnrichmentRequest(
        "album_catalog", "mb", "album", "catalog", 1,
        provider="musicbrainz",
    )
    enrichment._store_job(object(), "u", request, {"state": "resolved"})
    success_payload = json.loads(connection.values[-1][3])
    enrichment._store_job(object(), "u", request, {"state": "retryable"})
    retry_payload = json.loads(connection.values[-1][3])
    assert success_payload["expires_at"] - success_payload["updated_at"] == enrichment.MUSICBRAINZ_RESULT_TTL_SECONDS
    assert retry_payload["expires_at"] - retry_payload["updated_at"] == enrichment.ACQUISITION_RESULT_TTL_SECONDS


def test_musicbrainz_shared_backoff_does_not_block_other_providers(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(enrichment, "_load_job", lambda *_args: None)
    monkeypatch.setattr(enrichment, "_store_job", lambda *_args: None)
    monkeypatch.setattr(
        enrichment,
        "_fetch_request",
        lambda _server, request: calls.append(request.kind) or [],
    )
    plan = CandidateEnrichmentPlan(
        user_scope_id="user-1",
        requests=[
            EnrichmentRequest(
                kind="album_catalog",
                provider="musicbrainz",
                key="artist-a",
                pool="album",
                relation="artist_album_catalog",
                limit=1,
                metadata={"profile_seed_artist": "Artist A"},
            ),
            EnrichmentRequest(
                kind="lastfm_artist_similar",
                key="artist-b",
                pool="artist_graph",
                relation="similar_artist",
                limit=8,
            ),
        ],
        prior_request_progress={
            "album_catalog:musicbrainz:album:failed-artist": {
                "state": "retryable",
                "last_error": "TimeoutError",
                "provider_retry_at": time.time() + 60,
            }
        },
    )

    result = materialize_enrichment_plan(
        object(), plan, max_workers=2, max_pending_jobs=2
    )

    assert calls == ["lastfm_artist_similar"]
    assert result.diagnostics["scheduled_request_count"] == 1
    assert result.diagnostics["deferred_request_count"] == 1


def test_transient_provider_backoff_is_shared_but_isolated(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(enrichment, "_load_job", lambda *_args: None)
    monkeypatch.setattr(enrichment, "_store_job", lambda *_args: None)
    monkeypatch.setattr(
        enrichment,
        "_fetch_request",
        lambda _server, request: calls.append(request.provider) or [],
    )
    plan = CandidateEnrichmentPlan(
        user_scope_id="user-1",
        requests=[
            EnrichmentRequest(
                kind="lastfm_artist_similar",
                provider="lastfm",
                key="artist-a",
                pool="artist_graph",
                relation="similar_artist",
                limit=8,
            ),
            EnrichmentRequest(
                kind="artist_catalog",
                provider="ytmusic",
                key="artist-b",
                pool="profile_spine",
                relation="same_artist_catalog",
                limit=8,
            ),
        ],
        prior_request_progress={
            "lastfm_artist_similar:lastfm:artist_graph:failed": {
                "state": "retryable",
                "provider": "lastfm",
                "provider_retry_at": time.time() + 60,
            }
        },
    )

    result = materialize_enrichment_plan(
        object(), plan, max_workers=2, max_pending_jobs=2
    )

    assert calls == ["ytmusic"]
    assert result.diagnostics["scheduled_request_count"] == 1
    assert result.diagnostics["deferred_request_count"] == 1


def test_http_provider_failure_preserves_status_and_retry_after() -> None:
    error = RuntimeError("upstream unavailable")
    error.response = SimpleNamespace(  # type: ignore[attr-defined]
        status_code=503,
        headers={"Retry-After": "45"},
    )

    details = enrichment._provider_failure_details(error)

    assert details["http_status"] == 503
    assert details["provider_retry_after_seconds"] == 45
    assert details["transient"] is True


def test_musicbrainz_failure_keeps_other_completed_catalog_job(monkeypatch) -> None:
    monkeypatch.setattr(enrichment, "_load_job", lambda *_args: None)
    monkeypatch.setattr(enrichment, "_store_job", lambda *_args: None)

    def provider(_server, request):
        if request.key == "artist-a":
            raise TimeoutError("temporary provider failure")
        return [
            {
                "id": "musicbrainz:release-group:album-b",
                "musicbrainz_release_group_id": "album-b",
                "title": "Album B",
                "artist": "Artist B",
                "canonical_tracks": [],
            }
        ]

    monkeypatch.setattr(enrichment, "_fetch_request", provider)
    plan = CandidateEnrichmentPlan(
        user_scope_id="user-1",
        requests=[
            EnrichmentRequest(
                kind="album_catalog",
                provider="musicbrainz",
                key=key,
                pool="album",
                relation="artist_album_catalog",
                limit=1,
                metadata={"profile_seed_artist": key},
            )
            for key in ("artist-a", "artist-b")
        ],
    )

    result = materialize_enrichment_plan(
        object(), plan, max_workers=2, max_pending_jobs=2
    )

    assert result.diagnostics["scheduled_request_count"] == 1
    assert result.diagnostics["completed_request_count"] == 0
    assert result.diagnostics["failed_request_count"] == 1
    assert result.diagnostics["raw_pool_counts"].get("album", 0) == 0


def test_expired_retained_supply_and_broken_radio_gate_reach_ready_queue(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Regression for the observed expired-candidate/22-card radio state."""
    now = time.time()
    track = {
        "id": "track:retained",
        "title": "Retained Track",
        "artist": "Artist One",
        "playable": True,
        "playable_source_id": "yt-retained",
        "provider": "ytmusic",
    }
    album = {
        "id": "musicbrainz:release-group:album-1",
        "musicbrainz_release_group_id": "album-1",
        "title": "Retained Album",
        "artist": "Artist One",
        "playable": True,
        "track_count": 2,
        "canonical_tracks": [track, track],
    }
    from auralis_backend.discovery.schema import DiscoveryCandidate  # noqa: E402

    expired = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=now - CANDIDATE_SNAPSHOT_TTL_SECONDS - 1,
        expires_at=now - 1,
        generation_id="expired-generation",
        pools={
            "similarity": [DiscoveryCandidate(track, "similarity", 1.0, item_type="track")],
            "album": [DiscoveryCandidate(album, "album", 1.0, item_type="album") for _ in range(4)],
        },
        coverage={"ready": True},
    )
    seed = candidate_inventory_refresh_seed(expired, reason="ttl_expired")
    assert seed.coverage["ready"] is True
    assert seed.pools["album"]
    assert "radio_artist_catalog" not in seed.pools

    # The radio service must use the persisted reservoir repair before asking
    # for a candidate-ready inventory or invoking a provider.
    broken = _radio_inventory(visible=False)
    ready = _radio_inventory(visible=True)
    loaded = [broken, ready]
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.load_candidate_inventory",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.load_artist_radio_inventory",
        lambda *_args, **_kwargs: loaded.pop(0),
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.select_radio_rotation",
        lambda *_args, **_kwargs: ready,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.merge_store_artist_radio_inventory",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.radio_supply.materialize_enrichment_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("retained radio supply must not acquire")
        ),
    )
    outcome = RadioSupplyService(
        object(), HomeRepositorySet(_factory(tmp_path / "failure-shape.sqlite"))
    ).refresh(
        TasteProfile(
            user_scope_id="user-1",
            profile_key="profile-1",
            signal_tier="established",
        )
    )
    assert outcome.ready is True
    assert outcome.scheduled_requests == 0
    assert outcome.reason == "ready_reservoir_reused"


def test_acquisition_deadline_is_a_real_wall_clock_limit(monkeypatch) -> None:
    monkeypatch.setattr(enrichment, "_load_job", lambda *_args: None)
    monkeypatch.setattr(enrichment, "_store_job", lambda *_args: None)

    def slow_provider(*_args):
        time.sleep(2.0)
        return []

    monkeypatch.setattr(enrichment, "_fetch_request", slow_provider)
    request = EnrichmentRequest(
        kind="album_catalog",
        provider="musicbrainz",
        key="Slow Artist",
        pool="album",
        relation="artist_album_catalog",
        limit=4,
        metadata={"profile_seed_artist": "Slow Artist"},
    )
    started = time.perf_counter()
    result = materialize_enrichment_plan(
        object(),
        CandidateEnrichmentPlan(user_scope_id="user-1", requests=[request]),
        time_budget_seconds=1.0,
        max_workers=1,
        max_pending_jobs=1,
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 1.75
    assert result.diagnostics["timed_out_request_count"] == 1


def test_musicbrainz_provider_error_is_retryable_not_catalog_exhaustion(monkeypatch) -> None:
    monkeypatch.setattr(enrichment, "_load_job", lambda *_args: None)
    monkeypatch.setattr(enrichment, "_store_job", lambda *_args: None)

    def provider_error(*_args, **_kwargs):
        raise RuntimeError("musicbrainz temporarily unavailable")

    monkeypatch.setattr(
        "auralis_backend.search.musicbrainz.browse_musicbrainz_artist_album_items",
        provider_error,
    )
    request = EnrichmentRequest(
        kind="album_catalog",
        provider="musicbrainz",
        key="artist-1",
        pool="album",
        relation="artist_album_catalog",
        limit=4,
        metadata={
            "profile_seed_artist": "Artist One",
            "musicbrainz_artist_id": "artist-1",
        },
    )
    result = materialize_enrichment_plan(
        object(),
        CandidateEnrichmentPlan(user_scope_id="user-1", requests=[request]),
        max_workers=1,
        max_pending_jobs=1,
    )
    assert result.diagnostics["failure_types"]["album_catalog:musicbrainz:artist-1"] == "RuntimeError"
    progress = result.diagnostics["request_progress"][
        "album_catalog:musicbrainz:album:artist-1"
    ]
    assert progress["state"] == "retryable"
    assert progress.get("exhausted") is not True


def _rows(epoch: int, radio_cards: list[dict]) -> list[DiscoveryRow]:
    counts = {
        "todays_pick": 6,
        "featured_new_albums": 8,
        "last_played": 8,
        "frequently_listened": 8,
        "made_for_you": 8,
        "because_you_played": 12,
        "recommended_albums": 12,
        "recommended_artists": 10,
        "quiet_picks": 20,
    }
    output: list[DiscoveryRow] = []
    for kind in (
        "todays_pick",
        "featured_new_albums",
        "last_played",
        "frequently_listened",
        "made_for_you",
        "because_you_played",
        "popular_radio",
        "recommended_albums",
        "recommended_artists",
        "quiet_picks",
    ):
        if kind == "popular_radio":
            mixes = [
                dict(card)
                for card in radio_cards
                if str(card.get("radio_mode") or "") == "multi_artist_mix"
            ]
            discovery = [
                dict(card)
                for card in radio_cards
                if str(card.get("radio_mode") or "") != "multi_artist_mix"
                and str(card.get("seed_affinity") or "")
                in {"related", "exploratory"}
            ]
            familiar = [
                dict(card)
                for card in radio_cards
                if str(card.get("radio_mode") or "") != "multi_artist_mix"
                and str(card.get("seed_affinity") or "")
                not in {"related", "exploratory"}
            ]
            offset = (max(int(epoch or 0), 0) * 5) % max(len(familiar), 1)
            familiar = familiar[offset:] + familiar[:offset]
            items = [*mixes[:2], *discovery[:4], *familiar[:6]]
        else:
            items = [
                {"id": f"epoch-{epoch}:{kind}:{index}"}
                for index in range(counts[kind])
            ]
        output.append(
            DiscoveryRow(
                id=kind,
                title=kind,
                kind=kind,
                item_type="radio" if kind == "popular_radio" else "track",
                items=items,
            )
        )
    return output


def test_immutable_feed_build_meets_time_and_quality_budget(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "build-budget.sqlite"))
    now = time.time()
    candidates = [
        CandidateInventory(
            user_scope_id="user-1",
            profile_fingerprint="profile-1",
            generated_at=now - index,
            expires_at=now - index + CANDIDATE_SNAPSHOT_TTL_SECONDS,
            generation_id=f"candidate-generation-{index + 1}",
            coverage={"ready": True},
        )
        for index in range(2)
    ]
    radio = _radio_inventory(visible=True)
    for candidate in candidates:
        repositories.candidates.put(
            CandidateSnapshotRecord(
                snapshot_id=candidate.generation_id,
                user_scope_id=candidate.user_scope_id,
                profile_fingerprint=candidate.profile_fingerprint,
                generated_at=candidate.generated_at,
                expires_at=candidate.expires_at,
                payload=candidate_inventory_to_payload(candidate),
            )
        )
    repositories.radio.put(
        RadioSnapshotRecord(
            snapshot_id=radio.generation_id,
            user_scope_id=radio.user_scope_id,
            profile_fingerprint=radio.profile_fingerprint,
            generated_at=radio.generated_at,
            expires_at=radio.expires_at,
            payload=artist_radio_inventory_to_payload(radio),
        )
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.composer.build_rows_from_pools",
        lambda _pools, taste: (
            _rows(taste.rotation_epoch, list(radio.reservoir_cards)),
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
    taste = TasteProfile(
        user_scope_id="user-1",
        profile_key="profile-1",
        signal_tier="established",
    )

    timings = []
    outcomes = []
    for candidate in candidates:
        started = time.perf_counter()
        outcomes.append(
            composer.compose(
                taste,
                reason="build_budget",
                candidate_snapshot_id=candidate.generation_id,
                radio_snapshot_id=radio.generation_id,
            )
        )
        timings.append(time.perf_counter() - started)

    assert [outcome.reason for outcome in outcomes] == [
        "active_initialized",
        "ready_appended",
    ]
    assert max(timings) < BUILD_BUDGET_SECONDS
    for outcome in outcomes:
        record = repositories.artifacts.get(outcome.artifact_id)
        assert record is not None
        assert is_complete_feed_bundle_payload(record.payload) is True
        assert evaluate_feed_contract(
            record.payload["rows"],
            earned_row_kinds={
                "last_played",
                "frequently_listened",
                "because_you_played",
            },
        ).accepted is True
        popular_radio = next(
            row for row in record.payload["rows"] if row["kind"] == "popular_radio"
        )
        assert evaluate_popular_radio_contract(popular_radio["items"]).accepted
        assert record.payload["diagnostics"]["feed_external_calls"] == 0


def test_unchanged_radio_does_not_block_fresh_feed_rows(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    repositories = HomeRepositorySet(_factory(tmp_path / "similar-radio.sqlite"))
    now = time.time()
    candidates = [
        CandidateInventory(
            user_scope_id="user-1",
            profile_fingerprint="profile-1",
            generated_at=now - index,
            expires_at=now - index + CANDIDATE_SNAPSHOT_TTL_SECONDS,
            generation_id=f"candidate-generation-{index + 1}",
            coverage={"ready": True},
        )
        for index in range(3)
    ]
    radio = _radio_inventory(visible=True)
    for candidate in candidates:
        repositories.candidates.put(
            CandidateSnapshotRecord(
                snapshot_id=candidate.generation_id,
                user_scope_id=candidate.user_scope_id,
                profile_fingerprint=candidate.profile_fingerprint,
                generated_at=candidate.generated_at,
                expires_at=candidate.expires_at,
                payload=candidate_inventory_to_payload(candidate),
            )
        )
    repositories.radio.put(
        RadioSnapshotRecord(
            snapshot_id=radio.generation_id,
            user_scope_id=radio.user_scope_id,
            profile_fingerprint=radio.profile_fingerprint,
            generated_at=radio.generated_at,
            expires_at=radio.expires_at,
            payload=artist_radio_inventory_to_payload(radio),
        )
    )
    monkeypatch.setattr(
        "auralis_backend.home_feed.composer.build_rows_from_pools",
        lambda _pools, taste: (
            _rows(taste.rotation_epoch, list(radio.cards)),
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
    taste = TasteProfile(
        user_scope_id="user-1",
        profile_key="profile-1",
        signal_tier="established",
    )

    first = composer.compose(
        taste,
        reason="similarity-regression",
        candidate_snapshot_id=candidates[0].generation_id,
        radio_snapshot_id=radio.generation_id,
    )
    second = composer.compose(
        taste,
        reason="similarity-regression",
        candidate_snapshot_id=candidates[1].generation_id,
        radio_snapshot_id=radio.generation_id,
    )

    assert first.reason == "active_initialized"
    assert second.reason == "ready_appended"
    assert len(repositories.state.get("user-1").ready_artifact_ids) == 1

    # A retry that changes neither radio nor the other rows remains blocked.
    monkeypatch.setattr(
        "auralis_backend.home_feed.composer.build_rows_from_pools",
        lambda _pools, _taste: (
            _rows(2, list(radio.cards)),
            {},
            {},
            {"accepted": True},
        ),
    )
    third = composer.compose(
        taste,
        reason="similarity-regression",
        candidate_snapshot_id=candidates[2].generation_id,
        radio_snapshot_id=radio.generation_id,
    )
    assert third.reason == "artifact_unchanged"
    assert len(repositories.state.get("user-1").ready_artifact_ids) == 1
