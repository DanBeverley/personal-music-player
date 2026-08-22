from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

from auralis_backend.discovery.config import ROW_RECIPES
from auralis_backend.discovery.feed_state import _completed_optional_rows
from auralis_backend.discovery.radio_inventory import (
    ArtistRadioInventory,
    RADIO_RESERVOIR_TARGET_CARDS,
    _load_radio_catalog_index,
    build_artist_radio_inventory,
    load_artist_radio_inventory,
    merge_radio_reservoirs,
    radio_card_candidates,
    select_radio_rotation,
    store_artist_radio_inventory,
    merge_store_artist_radio_inventory,
)
from auralis_backend.discovery.radio_inventory import _merge_radio_catalog_index, _from_payload
from auralis_backend.recommend.store_runtime import open_recommendation_store_connection
from auralis_backend.discovery.schema import (
    DiscoveryArtifact,
    DiscoveryCandidate,
    DiscoveryRow,
    TasteProfile,
)
from auralis_backend.discovery.artifact import artifact_to_dict, _artifact_from_dict
from auralis_backend.discovery.service import DiscoveryService, _radio_inventory_matches_successor
from auralis_backend.discovery.feed_state import (
    FeedState,
    ReadyFeedEntry,
    load_feed_state,
    save_feed_state,
)
from auralis_backend.discovery.enrichment import build_enrichment_plan
from auralis_backend.discovery.inventory import (
    CandidateInventory,
    load_candidate_inventory,
    store_candidate_inventory,
)
from auralis_backend.storage.artist_artwork import attach_persisted_artist_artwork
from auralis_backend.storage.artist_artwork import artist_artwork_token
from auralis_backend.search.intelligence import load_catalog_artist_records


def _server(tmp_path):
    return SimpleNamespace(
        RECOMMENDATION_STORE_DB_PATH=str(tmp_path / "recommendation.sqlite3"),
        recommendation_store_lock=threading.RLock(),
    )


def _verified_card(index: int) -> dict:
    identity = f"provider:artist:uc{index:04d}"
    artwork = f"/artist_artwork/{artist_artwork_token(identity)}"
    return {
        "id": f"radio-{index}",
        "thumbnail": artwork,
        "collage_images": [artwork],
        "tracks": [{"id": f"track-{index}-{track}"} for track in range(12)],
        "seed_artist_identity": identity,
        "seed_artist_identity_token": artist_artwork_token(identity),
        "artwork_owner_identity": identity,
        "artwork_owner_token": artwork.rsplit("/", 1)[-1],
    }


def _inventory(*, expires_at: float, verified: bool = True) -> ArtistRadioInventory:
    cards = [_verified_card(index) for index in range(12)]
    if not verified:
        cards[0]["thumbnail"] = ""
        cards[0]["collage_images"] = []
    return ArtistRadioInventory(
        user_scope_id="scope",
        profile_fingerprint="profile",
        generated_at=time.time() - 10,
        expires_at=expires_at,
        generation_id="radio-generation",
        cards=cards,
    )


def _taste(epoch: int) -> TasteProfile:
    artists = [f"Artist {index}" for index in range(12)]
    return TasteProfile(
        user_scope_id="scope",
        profile_key="profile",
        signal_tier="personalized",
        top_artists=artists,
        rotation_epoch=epoch,
    )


def _radio_pool() -> list[DiscoveryCandidate]:
    output: list[DiscoveryCandidate] = []
    for artist_index in range(12):
        artist = f"Artist {artist_index}"
        artist_id = f"UC{artist_index:04d}"
        for track_index in range(12):
            output.append(
                DiscoveryCandidate(
                    item={
                        "id": f"track-{artist_index}-{track_index}",
                        "title": f"Track {artist_index}-{track_index}",
                        "artist": artist,
                        "artist_id": artist_id,
                        "radio_seed_artist": artist,
                        "artist_thumbnail": f"https://images.example/{artist_index}.jpg",
                        "playable": True,
                        "playable": True,
                    },
                    source="radio_artist_catalog",
                    score=1.0,
                    item_type="track",
                )
            )
    return output


def test_radio_inventory_requires_verified_artwork_and_filters_publication() -> None:
    incomplete = _inventory(expires_at=time.time() + 60, verified=False)
    assert incomplete.is_ready is False
    assert radio_card_candidates(incomplete) == []
    assert _inventory(expires_at=time.time() + 60).is_ready is True


def test_radio_selector_uses_minimum_target_and_variable_bonus_counts() -> None:
    inventory = _inventory(expires_at=time.time() + 60)
    minimum = select_radio_rotation(inventory, epoch=0)
    assert minimum is not None and len(minimum.cards) == 12

    inventory.reservoir_cards = [
        *inventory.cards,
        *[_verified_card(index) for index in range(12, 24)],
    ]
    target = select_radio_rotation(inventory, epoch=0)
    bonus = select_radio_rotation(inventory, epoch=4)
    assert target is not None and len(target.cards) == 16
    assert bonus is not None and len(bonus.cards) == 20
    assert bonus.diagnostics["visible_minimum_count"] == 12
    assert bonus.diagnostics["visible_target_count"] == 16
    assert bonus.diagnostics["visible_maximum_count"] == 20


def test_radio_selector_keeps_discovery_quota_when_supply_exists() -> None:
    inventory = _inventory(expires_at=time.time() + 60)
    inventory.cards = [_verified_card(index) for index in range(20)]
    for card in inventory.cards[:4]:
        card["seed_affinity"] = "exploratory"
    inventory.reservoir_cards = list(inventory.cards)

    selected = select_radio_rotation(inventory, epoch=0)

    assert selected is not None and selected.is_ready
    assert len(selected.cards) == 16
    assert sum(
        card.get("seed_affinity") in {"related", "exploratory"}
        for card in selected.cards
    ) >= 4
    assert RADIO_RESERVOIR_TARGET_CARDS == 36


def test_weak_backfill_artist_alias_cannot_cross_artist_identity(tmp_path) -> None:
    server = _server(tmp_path)
    connection = open_recommendation_store_connection(server)
    now = time.time()
    try:
        connection.execute(
            """INSERT INTO catalog_entities(
                entity_type, entity_key, display_title, confidence,
                payload_json, updated_at
            ) VALUES ('artist', ?, ?, 0.99, ?, ?)""",
            [
                "musicbrainz:artist:nirvana",
                "Nirvana",
                json.dumps(
                    {
                        "name": "Nirvana",
                        "canonical_artist_id": "musicbrainz:artist:nirvana",
                    }
                ),
                now,
            ],
        )
        for alias, source in (
            ("sam smith", "catalog_backfill_search_event"),
            ("nirvana", "selected_artist"),
        ):
            connection.execute(
                """INSERT INTO catalog_entity_aliases(
                    alias_key, entity_type, entity_key, score, confidence,
                    source, updated_at
                ) VALUES (?, 'artist', ?, 1, 0.99, ?, ?)""",
                [alias, "musicbrainz:artist:nirvana", source, now],
            )
        connection.commit()
    finally:
        connection.close()

    records = load_catalog_artist_records(
        server,
        artist_names=["Sam Smith", "Nirvana"],
    )
    assert "sam smith" not in records
    assert records["nirvana"]["name"] == "Nirvana"
    connection = open_recommendation_store_connection(server)
    try:
        aliases = {
            row["alias_key"]: row["source"]
            for row in connection.execute(
                "SELECT alias_key, source FROM catalog_entity_aliases"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "sam smith" not in aliases
    assert aliases["nirvana"] == "selected_artist"


def test_related_artist_catalog_becomes_an_independent_radio_card(monkeypatch) -> None:
    direct_artists = [f"Artist {index}" for index in range(11)]
    neighbor = "Discovery Artist"
    taste = TasteProfile(
        user_scope_id="scope",
        profile_key="profile",
        signal_tier="personalized",
        top_artists=direct_artists,
        rotation_epoch=0,
    )
    pool: list[DiscoveryCandidate] = []
    all_artists = [*direct_artists, neighbor]
    for artist_index, artist in enumerate(all_artists):
        artist_id = f"UC{artist_index:04d}"
        for track_index in range(12):
            item = {
                "id": f"track-{artist_index}-{track_index}",
                "title": f"Track {artist_index}-{track_index}",
                "artist": artist,
                "artist_id": artist_id,
                "radio_seed_artist": artist if artist != neighbor else direct_artists[0],
                "playable": True,
            }
            if artist == neighbor:
                item.update(
                    {
                        "radio_catalog_role": "neighbor",
                        "related_to_artist": direct_artists[0],
                    }
                )
            pool.append(
                DiscoveryCandidate(
                    item=item,
                    source="radio_artist_catalog",
                    score=1.0,
                    item_type="track",
                )
            )

    monkeypatch.setattr(
        "auralis_backend.discovery.radio_inventory.load_catalog_artist_records",
        lambda _server, *, artist_names: {
            str(name).casefold(): {
                "id": f"UC{all_artists.index(str(name)):04d}",
                "provider_artist_id": f"UC{all_artists.index(str(name)):04d}",
                "name": str(name),
                "artwork_cache_identity": (
                    f"provider:artist:uc{all_artists.index(str(name)):04d}"
                ),
            }
            for name in artist_names
        },
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.radio_inventory.attach_persisted_artist_artwork",
        lambda _server, artist: {
            **artist,
            "thumbnail": (
                f"/artist_artwork/{artist_artwork_token(artist['artwork_cache_identity'])}"
            ),
        },
    )

    inventory = build_artist_radio_inventory(
        taste,
        {"radio_artist_catalog": pool},
        server=object(),
    )
    discovery_card = next(
        card
        for card in inventory.reservoir_cards
        if card.get("seed_artist_key") == "discovery artist"
    )
    assert discovery_card["seed_affinity"] == "related"
    assert {
        track["artist"] for track in discovery_card["tracks"]
    } == {neighbor}
    assert len(discovery_card["tracks"]) >= 12


def test_radio_inventory_rejects_legacy_version_and_mismatched_seed_artwork() -> None:
    assert _from_payload({"radio_inventory_version": "artist-radio-inventory-v1"}) is None
    inventory = _inventory(expires_at=time.time() + 60)
    inventory.cards[0]["artwork_owner_token"] = "different-owner"
    assert inventory.is_ready is False


def test_persisted_radio_jobs_backfill_idempotent_playable_canonical_index(tmp_path) -> None:
    server = _server(tmp_path)
    connection = open_recommendation_store_connection(server)
    try:
        rows = [
            {
                "kind": "canonical_artist_radio_catalog",
                "pool": "radio_artist_catalog",
                "key": "Artist One",
                "request_signature": "canonical_artist_radio_catalog:radio_artist_catalog:artist one:",
                "results": [
                    {"id": "provider-a", "title": "Song", "artist": "Artist One", "musicbrainz_recording_id": "rec-1", "playable": True, "radio_seed_artist": "Artist One"},
                    {"id": "provider-b", "title": "Song (duplicate)", "artist": "Artist One", "musicbrainz_recording_id": "rec-1", "playable": True, "radio_seed_artist": "Artist One"},
                    {"id": "unplayable", "title": "No Source", "artist": "Artist One", "musicbrainz_recording_id": "rec-2", "playable": False, "radio_seed_artist": "Artist One"},
                ],
            }
        ]
        for index, payload in enumerate(rows):
            connection.execute(
                "INSERT INTO recommendation_feature_store(namespace, entity_id, model_id, payload_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                ["discovery_acquisition_job", f"job-{index}", "structured-acquisition", __import__("json").dumps(payload), time.time()],
            )
        connection.commit()
    finally:
        connection.close()
    first = _merge_radio_catalog_index(server, "scope", {})
    second = _merge_radio_catalog_index(server, "scope", first)
    assert len(first["artist one"]) == 1
    assert len(second["artist one"]) == 1
    assert second["artist one"][0]["musicbrainz_recording_id"] == "rec-1"


def test_prepared_feed_radio_fast_path_reads_compact_index_without_job_scan(
    tmp_path,
) -> None:
    server = _server(tmp_path)
    connection = open_recommendation_store_connection(server)
    try:
        connection.execute(
            "INSERT INTO recommendation_feature_store(namespace, entity_id, model_id, payload_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            [
                "discovery_radio_catalog_index",
                "scope:artist one",
                "radio-catalog-index-v1",
                __import__("json").dumps(
                    {
                        "version": "radio-catalog-index-v1",
                        "seed_key": "artist one",
                        "tracks": [
                            {
                                "id": "indexed-track",
                                "title": "Indexed",
                                "artist": "Artist One",
                                "playable": True,
                            }
                        ],
                    }
                ),
                time.time(),
            ],
        )
        # This row is intentionally newer, but the queue-critical fast path
        # must not scan acquisition history. Maintenance/backfill owns it.
        connection.execute(
            "INSERT INTO recommendation_feature_store(namespace, entity_id, model_id, payload_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            [
                "discovery_acquisition_job",
                "new-job",
                "structured-acquisition",
                __import__("json").dumps(
                    {
                        "kind": "canonical_artist_radio_catalog",
                        "pool": "radio_artist_catalog",
                        "key": "Artist Two",
                        "results": [
                            {
                                "id": "job-track",
                                "title": "Job",
                                "artist": "Artist Two",
                                "playable": True,
                            }
                        ],
                    }
                ),
                time.time() + 1,
            ],
        )
        connection.commit()
    finally:
        connection.close()

    merged = _load_radio_catalog_index(
        server,
        "scope",
        {
            "artist one": [
                {
                    "id": "current-track",
                    "title": "Current",
                    "artist": "Artist One",
                    "playable": True,
                }
            ]
        },
    )

    assert [_track["id"] for _track in merged["artist one"]] == [
        "indexed-track",
        "current-track",
    ]
    assert "artist two" not in merged


def test_radio_index_recovers_only_verified_semantic_playable_source(tmp_path) -> None:
    server = _server(tmp_path)
    raw = {
        "title": "Recovered Song",
        "artist": "Artist One",
        "musicbrainz_recording_id": "rec-recovered",
        "playable": False,
        "radio_seed_artist": "Artist One",
    }
    from auralis_backend.search.intelligence import catalog_entity_key

    connection = open_recommendation_store_connection(server)
    try:
        connection.execute(
            "INSERT INTO recommendation_feature_store(namespace, entity_id, model_id, payload_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            [
                "discovery_acquisition_job",
                "recover-job",
                "structured-acquisition",
                __import__("json").dumps(
                    {
                        "kind": "canonical_artist_radio_catalog",
                        "pool": "radio_artist_catalog",
                        "key": "Artist One",
                        "results": [raw],
                    }
                ),
                time.time(),
            ],
        )
        connection.execute(
            "INSERT INTO catalog_entity_sources(entity_type, entity_key, source_provider, source_key, source_authority, confidence, payload_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "track",
                catalog_entity_key("track", raw),
                "ytmusic",
                "video-recovered",
                "topic",
                0.99,
                __import__("json").dumps(
                    {
                        "verification_state": "verified",
                        "expires_at": time.time() + 60,
                    }
                ),
                time.time(),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    merged = _merge_radio_catalog_index(server, "scope", {})
    assert merged["artist one"][0]["source_id"] == "video-recovered"


def test_completed_radio_is_not_reused_for_a_different_successor_epoch() -> None:
    inventory = _inventory(expires_at=time.time() + 60)
    inventory.diagnostics["successor_rotation_epoch"] = 3
    assert _radio_inventory_matches_successor(
        inventory,
        profile_fingerprint="profile",
        successor_epoch=3,
    )
    assert not _radio_inventory_matches_successor(
        inventory,
        profile_fingerprint="profile",
        successor_epoch=4,
    )


def test_expired_radio_inventory_is_not_loaded(tmp_path) -> None:
    server = _server(tmp_path)
    expired = _inventory(expires_at=time.time() - 1)
    assert store_artist_radio_inventory(server, expired)
    assert load_artist_radio_inventory(server, "scope") is None


def test_radio_builder_reuses_verified_cache_and_rotates_by_successor_epoch(
    monkeypatch,
) -> None:
    records = {
        f"UC{index:04d}": {
            "id": f"UC{index:04d}",
                "provider_artist_id": f"UC{index:04d}",
                "name": f"Artist {index}",
                "artwork_cache_identity": f"provider:artist:uc{index:04d}",
        }
        for index in range(12)
    }
    monkeypatch.setattr(
        "auralis_backend.discovery.radio_inventory.load_catalog_artist_records",
        lambda _server, *, artist_names: {
            str(name).casefold(): records[f"UC{int(str(name).split()[-1]):04d}"]
            for name in artist_names
        },
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.radio_inventory.attach_persisted_artist_artwork",
        lambda _server, artist: {
            **artist,
            "thumbnail": (
                    f"/artist_artwork/{artist_artwork_token('provider:artist:' + str(artist['provider_artist_id']).casefold())}"
            ),
        },
    )
    monkeypatch.setattr(
        "auralis_backend.storage.artist_artwork.schedule_artist_artwork_cache",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("radio composition must not schedule network work")
        ),
    )
    pools = {"radio_artist_catalog": _radio_pool()}
    first = build_artist_radio_inventory(_taste(1), pools, server=object())
    second = build_artist_radio_inventory(_taste(2), pools, server=object())
    assert first.is_ready and second.is_ready
    assert [card["seed_artist_key"] for card in first.cards] != [
        card["seed_artist_key"] for card in second.cards
    ]
    assert first.diagnostics["verified_artwork_card_count"] == 12
    assert first.diagnostics["artwork_repair_scheduled_count"] == 0


def test_radio_builder_schedules_repair_but_does_not_publish_source_urls(
    monkeypatch,
) -> None:
    records = {
        f"UC{index:04d}": {
            "id": f"UC{index:04d}",
            "provider_artist_id": f"UC{index:04d}",
            "name": f"Artist {index}",
        }
                        for index in range(12)
    }
    monkeypatch.setattr(
        "auralis_backend.discovery.radio_inventory.load_catalog_artist_records",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.radio_inventory.attach_persisted_artist_artwork",
        lambda _server, artist: dict(artist),
    )
    inventory = build_artist_radio_inventory(
        _taste(1),
        {"radio_artist_catalog": _radio_pool()},
        server=object(),
    )
    assert inventory.is_ready is False
    assert inventory.diagnostics["artwork_repair_scheduled_count"] == 12
    assert len(inventory.artwork_repair_records) == 12
    assert radio_card_candidates(inventory) == []


def test_materialized_reservoir_produces_distinct_ready_rotations(
    monkeypatch,
) -> None:
    artist_count = 24
    taste = TasteProfile(
        user_scope_id="scope",
        profile_key="profile",
        signal_tier="personalized",
        top_artists=[f"Artist {index}" for index in range(artist_count)],
        rotation_epoch=1,
    )
    pool: list[DiscoveryCandidate] = []
    for artist_index in range(artist_count):
        artist = f"Artist {artist_index}"
        artist_id = f"UC{artist_index:04d}"
        for track_index in range(12):
            pool.append(
                DiscoveryCandidate(
                    item={
                        "id": f"track-{artist_index}-{track_index}",
                        "title": f"Track {artist_index}-{track_index}",
                        "artist": artist,
                        "artist_id": artist_id,
                        "radio_seed_artist": artist,
                        "playable": True,
                    },
                    source="radio_artist_catalog",
                    score=1.0,
                    item_type="track",
                )
            )
    monkeypatch.setattr(
        "auralis_backend.discovery.radio_inventory.load_catalog_artist_records",
        lambda _server, *, artist_names: {
            str(name).casefold(): {
                "id": f"UC{int(str(name).split()[-1]):04d}",
                    "provider_artist_id": f"UC{int(str(name).split()[-1]):04d}",
                    "name": str(name),
                    "artwork_cache_identity": f"provider:artist:uc{int(str(name).split()[-1]):04d}",
            }
            for name in artist_names
        },
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.radio_inventory.attach_persisted_artist_artwork",
        lambda _server, artist: {
            **artist,
            "artwork_cache_status": "cached",
            "thumbnail": (
                    f"/artist_artwork/{artist_artwork_token('provider:artist:' + str(artist['provider_artist_id']).casefold())}"
            ),
        },
    )

    reservoir = build_artist_radio_inventory(
        taste,
        {"radio_artist_catalog": pool},
        server=object(),
    )
    assert len(reservoir.reservoir_cards) >= 24
    first = select_radio_rotation(reservoir, epoch=1)
    assert first is not None and first.is_ready
    first_card_ids = {str(card["id"]) for card in first.cards}
    first_track_ids = {
        str(track["id"])
        for card in first.cards
        for track in card.get("tracks") or []
    }
    second = select_radio_rotation(
        reservoir,
        excluded_card_ids=first_card_ids,
        excluded_track_ids=first_track_ids,
        epoch=2,
    )
    assert second is not None and second.is_ready
    second_card_ids = {str(card["id"]) for card in second.cards}
    assert len(first_card_ids & second_card_ids) == second.diagnostics[
        "fallback_card_count"
    ]
    assert second.diagnostics["novel_card_count"] == min(
        len(reservoir.reservoir_cards) - len(first_card_ids),
        len(second.cards),
    )
    assert second.diagnostics["novel_card_ratio"] == (
        second.diagnostics["novel_card_count"] / len(second.cards)
    )
    assert second.diagnostics["reservoir_shortage"] is False


def test_reservoir_shortage_uses_quality_fallbacks_instead_of_blocking_feed() -> None:
    inventory = _inventory(expires_at=time.time() + 60)
    inventory.reservoir_cards = list(inventory.cards)
    used_cards = {str(card["id"]) for card in inventory.cards}
    selected = select_radio_rotation(
        inventory,
        excluded_card_ids=used_cards,
        epoch=2,
    )
    assert selected is not None
    assert selected.is_ready is True
    assert len(selected.cards) >= 12
    assert selected.diagnostics["novel_card_count"] == 0
    assert selected.diagnostics["fallback_card_count"] >= 12
    assert selected.diagnostics["fallback_card_overlap_count"] == selected.diagnostics["fallback_card_count"]
    assert selected.diagnostics["reservoir_shortage"] is False


def test_six_novel_cards_fill_successor_with_two_controlled_fallbacks() -> None:
    inventory = _inventory(expires_at=time.time() + 60)
    extra = [_verified_card(index) for index in range(8, 20)]
    inventory.reservoir_cards = [*inventory.cards, *extra]
    # Fourteen cards are claimed by the active/queued feeds; six different
    # cards remain novel and the selector fills the rest with fallbacks.
    used_cards = {str(card["id"]) for card in inventory.reservoir_cards[:14]}
    selected = select_radio_rotation(
        inventory,
        excluded_card_ids=used_cards,
        epoch=3,
    )
    assert selected is not None and selected.is_ready
    assert 12 <= len(selected.cards) <= 20
    assert selected.diagnostics["novel_card_count"] >= 6
    assert selected.diagnostics["fallback_card_count"] == len(selected.cards) - selected.diagnostics["novel_card_count"]
    assert selected.diagnostics["fallback_card_overlap_count"] == selected.diagnostics[
        "fallback_card_count"
    ]
    assert selected.diagnostics["novel_card_ratio"] == (
        selected.diagnostics["novel_card_count"] / len(selected.cards)
    )


def test_persisted_rotation_clears_stale_artwork_repair_blocker() -> None:
    inventory = _inventory(expires_at=time.time() + 60)
    inventory.diagnostics.update(
        {
            "artwork_repair_scheduled_count": 113,
            "artwork_repair_pending_count": 113,
            "artwork_repair_dispatched_count": 0,
        }
    )
    selected = select_radio_rotation(inventory, epoch=4)
    assert selected is not None and selected.is_ready
    # Selection itself must never make old unrelated repairs a publication
    # blocker; the service can still dispatch fresh bounded repairs separately.
    assert selected.diagnostics["artwork_repair_scheduled_count"] == 0
    assert selected.diagnostics["artwork_repair_pending_count"] == 0


def test_artwork_reconcile_without_inventory_is_terminal_local_noop(
    tmp_path,
    monkeypatch,
) -> None:
    server = _server(tmp_path)
    service = DiscoveryService(server)

    class ImmediateExecutor:
        def submit(self, callback):
            callback()

    service._prepare_executor = ImmediateExecutor()
    monkeypatch.setattr(
        "auralis_backend.discovery.service.load_candidate_inventory",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.schedule_catalog_population",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("artwork reconciliation must not acquire providers")
        ),
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.materialize_enrichment_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("artwork reconciliation must not materialize providers")
        ),
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.verify_materialized_supply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("artwork reconciliation must not verify providers")
        ),
    )
    active = DiscoveryArtifact(
        session_id="active",
        user_scope_id="scope",
        profile_key="profile",
        generated_at=time.time(),
        expires_at=time.time() + 60,
        rows=[],
        diagnostics={},
        candidate_pool_counts={},
        provider_timings_ms={},
        home_tab_lanes={},
        accepted=True,
    )
    assert save_feed_state(server, FeedState(user_scope_id="scope", active_feed=active))
    taste = _taste(1)
    request = SimpleNamespace(user_scope_id="scope", refresh_token="")
    service._schedule_preparation(request, taste, reason="radio_artwork_reconcile")
    state = load_feed_state(server, "scope")
    assert state is not None
    assert state.generation_status == "ready"
    assert state.retry_at == 0.0
    assert state.retry_reason == ""
    assert state.dirty_reasons == ["radio_inventory_needed"]
    assert state.preparation_lease_token == ""
    assert state.active_feed is not None and state.active_feed.session_id == "active"


def test_radio_reservoir_expansion_is_bounded_and_deduplicated(tmp_path) -> None:
    server = _server(tmp_path)
    service = DiscoveryService(server)
    submitted = []

    class CaptureExecutor:
        def submit(self, callback):
            submitted.append(callback)

    service._radio_reservoir_executor = CaptureExecutor()
    taste = _taste(1)
    inventory = _inventory(expires_at=time.time() + 60)
    assert service._schedule_radio_reservoir_expansion(
        SimpleNamespace(), taste, inventory
    ) is True
    assert service._schedule_radio_reservoir_expansion(
        SimpleNamespace(), taste, inventory
    ) is False
    assert len(submitted) == 1


def test_full_direct_reservoir_still_expands_for_discovery_deficit(tmp_path) -> None:
    server = _server(tmp_path)
    service = DiscoveryService(server)
    submitted = []

    class CaptureExecutor:
        def submit(self, callback):
            submitted.append(callback)

    service._radio_reservoir_executor = CaptureExecutor()
    inventory = _inventory(expires_at=time.time() + 60)
    inventory.reservoir_cards = [_verified_card(index) for index in range(24)]
    for card in inventory.reservoir_cards:
        card["seed_affinity"] = "direct"
    inventory.diagnostics.update(
        {
            "reservoir_size": 24,
            "discovery_card_count": 0,
            "discovery_deficit": 4,
        }
    )
    assert service._schedule_radio_reservoir_expansion(
        SimpleNamespace(), _taste(1), inventory
    ) is True
    assert len(submitted) == 1


def test_full_reservoir_admits_new_discovery_cards_without_dropping_slice(
    tmp_path,
) -> None:
    server = _server(tmp_path)
    current = _inventory(expires_at=time.time() + 120)
    current.reservoir_cards = [_verified_card(index) for index in range(36)]
    for card in current.reservoir_cards:
        card["seed_affinity"] = "direct"
    selected_ids = {card["id"] for card in current.cards}
    assert store_artist_radio_inventory(server, current)

    incoming = _inventory(expires_at=time.time() + 120)
    incoming.reservoir_cards = [_verified_card(index) for index in range(100, 104)]
    for card in incoming.reservoir_cards:
        card["seed_affinity"] = "related"
    assert merge_store_artist_radio_inventory(server, incoming)

    merged = load_artist_radio_inventory(
        server,
        "scope",
        profile_fingerprint="profile",
    )
    assert merged is not None
    assert len(merged.reservoir_cards) == 36
    assert selected_ids <= {card["id"] for card in merged.cards}
    assert {
        card["id"]
        for card in incoming.reservoir_cards
    } <= {card["id"] for card in merged.reservoir_cards}
    assert merged.diagnostics["discovery_deficit"] == 8


def test_radio_reservoir_merge_preserves_newer_selected_slice(tmp_path) -> None:
    server = _server(tmp_path)
    current = _inventory(expires_at=time.time() + 120)
    current.cards = [dict(current.cards[0])]
    current.diagnostics["successor_rotation_epoch"] = 9
    assert store_artist_radio_inventory(server, current)
    incoming = _inventory(expires_at=time.time() + 120)
    incoming.generation_id = "older-expansion"
    incoming.reservoir_cards = [
        *incoming.cards,
        *[_verified_card(index) for index in range(8, 20)],
    ]
    assert merge_store_artist_radio_inventory(server, incoming)
    merged = load_artist_radio_inventory(server, "scope", profile_fingerprint="profile")
    assert merged is not None
    assert [card["id"] for card in merged.cards] == [current.cards[0]["id"]]
    assert len(merged.reservoir_cards) == 20
    assert merged.diagnostics["successor_rotation_epoch"] == 9


def test_artifact_lane_map_persists_once_and_round_trips() -> None:
    artifact = DiscoveryArtifact(
        session_id="s", user_scope_id="scope", profile_key="p",
        generated_at=1.0, expires_at=2.0, rows=[],
        diagnostics={"home_tab_lanes": {"mixes": {"ids": ["a"]}}},
        candidate_pool_counts={}, provider_timings_ms={},
        home_tab_lanes={"mixes": {"ids": ["a"]}}, accepted=True,
        quality_reasons=[], artifact_source="test",
    )
    payload = artifact_to_dict(artifact)
    assert "home_tab_lanes" not in payload["diagnostics"]
    restored = _artifact_from_dict(payload)
    assert restored is not None
    assert restored.home_tab_lanes == artifact.home_tab_lanes
    assert restored.diagnostics["home_tab_lanes"] == artifact.home_tab_lanes


def test_second_radio_expansion_consumes_first_progress_cursor(tmp_path) -> None:
    server = _server(tmp_path)
    first = _inventory(expires_at=time.time() + 120)
    first.diagnostics["radio_expansion_progress"] = {
        "anchor_cursor_next": 7,
        "artist_cursor_next": 4,
        "request_progress": {"batch": 1},
    }
    assert store_artist_radio_inventory(server, first)
    loaded = load_artist_radio_inventory(server, "scope", profile_fingerprint="profile")
    assert loaded is not None
    progress = loaded.diagnostics.get("radio_expansion_progress") or {}
    assert progress["anchor_cursor_next"] == 7
    assert progress["artist_cursor_next"] == 4
    assert progress["request_progress"] == {"batch": 1}


def test_stale_radio_expansion_keeps_newer_progress_revision(tmp_path) -> None:
    server = _server(tmp_path)
    current = _inventory(expires_at=time.time() + 120)
    current.diagnostics["radio_expansion_progress"] = {
        "anchor_cursor_next": 9,
        "artist_cursor_next": 6,
        "request_progress": {"batch": 2},
        "progress_revision": 2,
    }
    assert store_artist_radio_inventory(server, current)
    stale = _inventory(expires_at=time.time() + 120)
    stale.generation_id = "stale-expansion"
    stale.reservoir_cards = [*stale.cards, _verified_card(20)]
    stale.diagnostics["radio_expansion_progress"] = {
        "anchor_cursor_next": 3,
        "artist_cursor_next": 1,
        "request_progress": {"batch": 1},
        "progress_base_revision": 1,
        "progress_revision": 2,
    }
    assert merge_store_artist_radio_inventory(server, stale)
    loaded = load_artist_radio_inventory(server, "scope", profile_fingerprint="profile")
    assert loaded is not None
    progress = loaded.diagnostics.get("radio_expansion_progress") or {}
    assert progress["anchor_cursor_next"] == 9
    assert progress["artist_cursor_next"] == 6
    assert progress["request_progress"] == {"batch": 2}
    assert progress["progress_revision"] == 2
    assert any(card.get("id") == "radio-20" for card in loaded.reservoir_cards)


def test_radio_expansion_callbacks_preserve_feed_and_candidate_and_advance_cursor(
    tmp_path, monkeypatch
) -> None:
    server = _server(tmp_path)
    now = time.time()
    candidate = CandidateInventory(
        user_scope_id="scope",
        profile_fingerprint="profile",
        generated_at=now,
        expires_at=now + 600,
        generation_id="candidate-generation",
        coverage={"ready": True},
    )
    assert store_candidate_inventory(server, candidate)
    assert save_feed_state(
        server,
        FeedState(user_scope_id="scope", generation_status="ready"),
    )
    radio = _inventory(expires_at=now + 600)
    radio.reservoir_cards = list(radio.cards)
    radio.diagnostics.update({"reservoir_size": 8})
    assert store_artist_radio_inventory(server, radio)

    service = DiscoveryService(server)
    callbacks = []
    ledgers = []
    pass_index = {"value": 0}

    class CaptureExecutor:
        def submit(self, callback):
            callbacks.append(callback)

    service._radio_reservoir_executor = CaptureExecutor()

    def capture_plan(_taste, *, acquisition_ledger=None, allowed_pools=None,
                     radio_discovery_artist_seeds=None, radio_discovery_deficit=0):
        ledgers.append(dict(acquisition_ledger or {}))
        assert allowed_pools == {"radio_artist_catalog"}
        return SimpleNamespace(requests=[])

    def materialized(*_args, **_kwargs):
        pass_index["value"] += 1
        index = pass_index["value"]
        return SimpleNamespace(
            pools={},
            diagnostics={
                "anchor_cursor_next": 7 if index == 1 else 11,
                "artist_cursor_next": 4 if index == 1 else 6,
                "request_progress": {"batch": index},
                "completed_request_count": 1,
            },
        )

    def built_radio(*_args, **_kwargs):
        incoming = _inventory(expires_at=now + 600)
        incoming.reservoir_cards = [
            *incoming.cards,
            _verified_card(20 + pass_index["value"]),
        ]
        return incoming

    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_enrichment_plan", capture_plan
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.materialize_enrichment_plan", materialized
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.verify_materialized_supply",
        lambda _server, supply, *_args, **_kwargs: supply,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_candidate_inventory",
        lambda _server, _taste, *, previous, materialized_supply: previous,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_artist_radio_inventory", built_radio
    )

    taste = _taste(1)
    before_state = load_feed_state(server, "scope")
    assert service._schedule_radio_reservoir_expansion(
        SimpleNamespace(), taste, radio
    )
    callbacks.pop(0)()
    first = load_artist_radio_inventory(server, "scope", profile_fingerprint="profile")
    assert first is not None
    assert service._schedule_radio_reservoir_expansion(
        SimpleNamespace(), taste, first
    )
    callbacks.pop(0)()

    assert ledgers[1]["anchor_cursor"] == 7
    assert ledgers[1]["artist_cursor"] == 4
    assert ledgers[1]["request_progress"] == {"batch": 1}
    after_candidate = load_candidate_inventory(
        server, "scope", profile_fingerprint="profile"
    )
    after_state = load_feed_state(server, "scope")
    assert after_candidate is not None
    assert after_candidate.generation_id == "candidate-generation"
    assert before_state is not None and after_state is not None
    assert after_state.queue_revision == before_state.queue_revision
    assert after_state.generation_status == before_state.generation_status
    progress = (
        load_artist_radio_inventory(
            server, "scope", profile_fingerprint="profile"
        ).diagnostics.get("radio_expansion_progress")
        or {}
    )
    assert progress["anchor_cursor_next"] == 11
    assert progress["artist_cursor_next"] == 6
    assert progress["progress_revision"] == 2


def test_radio_expansion_stops_after_two_zero_qualified_cycles(tmp_path, monkeypatch) -> None:
    """Provider completion without qualified cards must be bounded and durable."""
    server = _server(tmp_path)
    now = time.time()
    candidate = CandidateInventory(
        user_scope_id="scope", profile_fingerprint="profile", generated_at=now,
        expires_at=now + 600, generation_id="candidate", coverage={"ready": True},
    )
    assert store_candidate_inventory(server, candidate)
    assert save_feed_state(server, FeedState(user_scope_id="scope", generation_status="ready"))
    radio = _inventory(expires_at=now + 600)
    radio.reservoir_cards = list(radio.cards)
    radio.diagnostics.update({"reservoir_size": 12, "discovery_deficit": 4})
    assert store_artist_radio_inventory(server, radio)

    service = DiscoveryService(server)
    callbacks = []
    class CaptureExecutor:
        def submit(self, callback):
            callbacks.append(callback)
    service._radio_reservoir_executor = CaptureExecutor()

    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_enrichment_plan",
        lambda *_a, **_k: SimpleNamespace(requests=[SimpleNamespace(
            kind="canonical_artist_radio_catalog",
            metadata={"radio_seed_artist": "Unqualified", "radio_seed_key": "provider:artist:u"},
        )]),
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.materialize_enrichment_plan",
        lambda *_a, **_k: SimpleNamespace(pools={}, diagnostics={"completed_request_count": 1}),
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.verify_materialized_supply",
        lambda _server, supply, *_a, **_k: supply,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_candidate_inventory",
        lambda _server, _taste, *, previous, materialized_supply: previous,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_artist_radio_inventory",
        lambda *_a, **_k: radio,
    )

    assert service._schedule_radio_reservoir_expansion(SimpleNamespace(), _taste(1), radio)
    callbacks.pop(0)()
    assert len(callbacks) == 1
    callbacks.pop(0)()

    persisted = load_artist_radio_inventory(server, "scope", profile_fingerprint="profile")
    assert persisted is not None
    progress = persisted.diagnostics.get("radio_expansion_progress") or {}
    assert progress["no_progress_cycles"] == 2
    assert progress["exhausted"] is True
    assert callbacks == []


def test_radio_expansion_excludes_ready_entry_and_keeps_stable_seed_key(
    tmp_path, monkeypatch
) -> None:
    server = _server(tmp_path)
    now = time.time()
    queued = DiscoveryCandidate(
        item={"artist": "Queued Artist", "artist_id": "provider:artist:queued",
              "relationship_provenance": "artist_graph"},
        source="artist_graph", score=1.0, item_type="track",
    )
    fresh = DiscoveryCandidate(
        item={"artist": "Fresh Artist", "artist_id": "provider:artist:fresh",
              "relationship_provenance": "artist_graph"},
        source="artist_graph", score=1.0, item_type="track",
    )
    candidate = CandidateInventory(
        user_scope_id="scope", profile_fingerprint="profile", generated_at=now,
        expires_at=now + 600, generation_id="candidate", coverage={"ready": True},
        pools={"artist_graph": [queued, fresh]},
    )
    assert store_candidate_inventory(server, candidate)
    artifact = DiscoveryArtifact(
        session_id="queued-feed", user_scope_id="scope", profile_key="profile",
        generated_at=now, expires_at=now + 600,
        rows=[DiscoveryRow(
            id="popular", title="Popular Radio", kind="popular_radio",
                item_type="artist_radio", items=[
                    {**_verified_card(900 + index),
                     "seed_artist_key": "queued artist" if index == 0 else f"other-{index}"}
                        for index in range(12)
                ],
            )], diagnostics={"feed_promotion_contract": "feed-promotion-v2-radio-verified"}, candidate_pool_counts={}, provider_timings_ms={},
        home_tab_lanes={}, accepted=True,
    )
    assert save_feed_state(server, FeedState(
        user_scope_id="scope",
        ready_feeds=[ReadyFeedEntry(artifact=artifact)],
        generation_status="ready",
    ))
    radio = _inventory(expires_at=now + 600)
    radio.diagnostics.update({"reservoir_size": 12, "discovery_deficit": 4})
    assert store_artist_radio_inventory(server, radio)

    captured = []
    service = DiscoveryService(server)
    callbacks = []
    class CaptureExecutor:
        def submit(self, callback):
            callbacks.append(callback)
    service._radio_reservoir_executor = CaptureExecutor()
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_enrichment_plan",
        lambda _taste, **kwargs: (captured.append(kwargs.get("radio_discovery_artist_seeds"))
                                  or SimpleNamespace(requests=[])),
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.materialize_enrichment_plan",
        lambda *_a, **_k: SimpleNamespace(pools={}, diagnostics={}),
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.verify_materialized_supply",
        lambda _server, supply, *_a, **_k: supply,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_candidate_inventory",
        lambda _server, _taste, *, previous, materialized_supply: previous,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_artist_radio_inventory",
        lambda *_a, **_k: radio,
    )
    assert service._schedule_radio_reservoir_expansion(SimpleNamespace(), _taste(1), radio)
    callbacks.pop(0)()
    assert captured and [seed["name"] for seed in captured[0]] == ["Fresh Artist"]
    assert captured[0][0]["key"] == "provider:artist:fresh"


def test_reservoir_merge_reuses_existing_and_improves_matching_cards() -> None:
    existing = _inventory(expires_at=time.time() + 60)
    existing.reservoir_cards = list(existing.cards)
    incoming = _inventory(expires_at=time.time() + 120)
    incoming.generation_id = "new-generation"
    incoming.cards[0] = {
        **incoming.cards[0],
        "tracks": [
            {"id": f"improved-{index}"} for index in range(12)
        ],
    }
    incoming.reservoir_cards = list(incoming.cards)
    merged = merge_radio_reservoirs(existing, incoming)
    assert len(merged.reservoir_cards) >= 12
    assert merged.reservoir_cards[0]["tracks"][0]["id"] == "improved-0"
    assert merged.diagnostics["reservoir_previous_size"] >= 12


def test_radio_artwork_repairs_dispatch_off_feed_critical_path(
    tmp_path,
    monkeypatch,
) -> None:
    server = _server(tmp_path)
    service = DiscoveryService(server)
    started = threading.Event()
    release = threading.Event()

    def slow_schedule(_server, artist, *, on_cached=None):
        started.set()
        assert release.wait(timeout=2.0)
        if on_cached is not None:
            on_cached(dict(artist))
        return True

    monkeypatch.setattr(
        "auralis_backend.discovery.service.schedule_artist_artwork_cache",
        slow_schedule,
    )
    taste = SimpleNamespace(user_scope_id="scope", profile_key="profile")
    before = time.perf_counter()
    count = service._schedule_radio_artwork_repairs(
        SimpleNamespace(),
        taste,
        [
            {"provider_artist_id": "artist-1", "name": "Artist One"},
            {"provider_artist_id": "artist-1", "name": "Artist One"},
        ],
    )
    elapsed = time.perf_counter() - before
    assert count == 1
    assert elapsed < 0.2
    assert started.wait(timeout=1.0)
    release.set()
    service._radio_artwork_executor.shutdown(wait=True)


def test_stale_radio_row_is_not_treated_as_complete() -> None:
    row = DiscoveryRow(
        id="popular_radio",
        title="Popular Radio",
        kind="popular_radio",
        item_type="radio",
        items=[
            {
                "id": f"radio-{index}",
                "thumbnail": "",
                "collage_images": [],
            }
            for index in range(ROW_RECIPES["popular_radio"].min_items)
        ],
    )
    artifact = DiscoveryArtifact(
        session_id="session",
        user_scope_id="scope",
        profile_key="profile",
        generated_at=time.time(),
        expires_at=time.time() + 60,
        rows=[row],
        diagnostics={},
        candidate_pool_counts={},
        provider_timings_ms={},
        home_tab_lanes={},
        accepted=True,
    )
    assert "popular_radio" not in _completed_optional_rows(artifact)


def test_radio_only_plan_targets_nearest_incomplete_seed_and_is_bounded() -> None:
    taste = _taste(1)
    plan = build_enrichment_plan(
        taste,
        acquisition_ledger={
            "failed_domains": ["popular_radio"],
            "optional_row_counts": {"popular_radio": 4},
            "radio_seed_counts": {
                "artist 0": 11,
                "artist 1": 10,
                "artist 2": 8,
                "artist 3": 12,
            },
        },
        allowed_pools={"radio_artist_catalog"},
    )
    requests = [
        request
        for request in plan.requests
        if request.kind == "canonical_artist_radio_catalog"
    ]
    assert 1 <= len(requests) <= 3
    assert [request.metadata["profile_seed_artist"] for request in requests[:3]] == [
        "Artist 0",
        "Artist 1",
        "Artist 2",
    ]


def test_persisted_artwork_migrates_identity_bound_legacy_record() -> None:
    identity = "provider:artist:ucverified"
    from auralis_backend.storage.artist_artwork import artist_artwork_token

    token = artist_artwork_token(identity)
    attached = attach_persisted_artist_artwork(
        object(),
        {
            "name": "Verified Artist",
            "provider_artist_id": "UCVerified",
            "artwork_cache_identity": identity,
            "artwork_cache_token": token,
            "thumbnail": f"/artist_artwork/{token}",
        },
    )
    assert attached["artwork_cache_status"] == "cached"
    assert attached["thumbnail"] == f"/artist_artwork/{token}"
