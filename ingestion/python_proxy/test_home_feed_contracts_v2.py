from __future__ import annotations

import pathlib
import sys

import pytest


CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.home_feed.contracts import (  # noqa: E402
    ACTIVE_ARTIFACT_TTL_SECONDS,
    CANDIDATE_SNAPSHOT_TTL_SECONDS,
    READY_ARTIFACT_TTL_SECONDS,
    CandidateSnapshotRecord,
    FeedArtifactRecord,
    evaluate_feed_contract,
    evaluate_popular_radio_contract,
    retry_delay_seconds,
)
from auralis_backend.discovery.radio_inventory import (  # noqa: E402
    ArtistRadioInventory,
    select_radio_rotation,
)
from auralis_backend.discovery.inventory import (  # noqa: E402
    CandidateInventory,
    canonicalize_candidate_pools,
)
from auralis_backend.discovery.config import ROW_RECIPES  # noqa: E402
from auralis_backend.discovery.ranking import rank_albums  # noqa: E402
from auralis_backend.discovery.schema import (  # noqa: E402
    DiscoveryArtifact,
    DiscoveryCandidate,
    DiscoveryRow,
    TasteProfile,
)
from auralis_backend.home_feed.composer import (  # noqa: E402
    _earned_row_kinds,
    _meaningfully_differs,
    _successor_novelty,
    _preserve_complete_optional_rows,
    artifact_exposure_entries,
)
from auralis_backend.home_feed import composer as home_composer  # noqa: E402
from auralis_backend.storage.artist_artwork import artist_artwork_token  # noqa: E402
from auralis_backend.home_feed.bundles import (  # noqa: E402
    build_feed_bundle_manifest,
    is_complete_feed_bundle_payload,
    with_feed_bundle_manifest,
)


def _row(kind: str, count: int) -> dict:
    return {"kind": kind, "items": [{"id": f"{kind}-{index}"} for index in range(count)]}


def _core_rows() -> list[dict]:
    return [
        _row("todays_pick", 6),
        _row("featured_new_albums", 8),
        _row("made_for_you", 8),
        _row("popular_radio", 12),
        _row("recommended_albums", 12),
        _row("recommended_artists", 10),
        _row("quiet_picks", 20),
    ]


def test_visible_exposure_partition_does_not_consume_deep_row_reserve() -> None:
    artifact = DiscoveryArtifact(
        session_id="feed-a",
        user_scope_id="user-1",
        profile_key="profile-1",
        generated_at=100,
        expires_at=1_000,
        rows=[
            DiscoveryRow(
                id="quiet_picks",
                title="Quiet picks",
                kind="quiet_picks",
                item_type="track",
                items=[{"id": f"track-{index}"} for index in range(40)],
            )
        ],
        home_tab_lanes=[],
        accepted=True,
        diagnostics={},
        candidate_pool_counts={},
        provider_timings_ms={},
    )

    visible = artifact_exposure_entries(artifact, visible_only=True)
    all_entries = artifact_exposure_entries(artifact)

    visible_ids = {identity for _, _, identity in visible}
    all_ids = {identity for _, _, identity in all_entries}
    assert "track-19" in visible_ids
    assert "track-20" not in visible_ids
    assert "track-20" in all_ids


def test_featured_album_exposure_records_artist_and_ranks_unseen_artist_first() -> None:
    artifact = DiscoveryArtifact(
        session_id="feed-albums",
        user_scope_id="user-1",
        profile_key="profile-1",
        generated_at=100,
        expires_at=1_000,
        rows=[
            DiscoveryRow(
                id="featured_new_albums",
                title="Featured albums",
                kind="featured_new_albums",
                item_type="album",
                items=[{"id": "album-seen", "title": "Seen", "artist": "Known Artist"}],
            )
        ],
        home_tab_lanes=[],
        accepted=True,
        diagnostics={},
        candidate_pool_counts={},
        provider_timings_ms={},
    )
    entries = artifact_exposure_entries(artifact, visible_only=True)
    assert ("featured_new_albums", "artist", "artist:known artist") in entries

    pools = {
        "album": [
            DiscoveryCandidate(
                item={"id": "album-seen-2", "title": "Seen Again", "artist": "Known Artist", "release_year": "2026"},
                source="album",
                score=10.0,
                item_type="album",
            ),
            DiscoveryCandidate(
                item={"id": "album-fresh", "title": "Fresh", "artist": "New Artist", "release_year": "2026"},
                source="album",
                score=1.0,
                item_type="album",
            ),
        ]
    }
    ranked, _ = rank_albums(
        pools,
        TasteProfile(
            user_scope_id="user-1",
            profile_key="profile-1",
            signal_tier="cold_start",
            force_refresh=True,
            avoid_ids=["artist:known artist"],
            hard_avoid_ids=["artist:known artist"],
        ),
        ROW_RECIPES["featured_new_albums"],
    )
    assert [item["artist"] for item in ranked[:2]] == ["New Artist", "Known Artist"]


def test_candidate_canonicalization_binds_each_real_pool_name() -> None:
    playable = DiscoveryCandidate(
        item={
            "id": "provider-track-1",
            "videoId": "provider-track-1",
            "provider": "ytmusic",
            "title": "Discovery Track",
            "artist": "Discovery Artist",
            "playable": True,
        },
        source="similarity",
        item_type="track",
    )
    history = DiscoveryCandidate(
        item={
            "id": "history-track-1",
            "title": "History Track",
            "artist": "History Artist",
        },
        source="history",
        item_type="track",
    )

    pools, stats = canonicalize_candidate_pools(
        {"similarity": [playable], "history": [history]}
    )

    assert len(pools["similarity"]) == 1
    assert len(pools["history"]) == 1
    assert stats["raw_track_count"] == 1
    assert stats["unplayable_track_count"] == 0


def test_immutable_bundle_manifest_covers_every_row_once_and_detects_tampering() -> None:
    rows = [
        {"id": f"row-{index}", "kind": f"row-{index}", "items": [{"id": index}]}
        for index in range(10)
    ]
    payload = with_feed_bundle_manifest(
        {"rows": rows, "accepted": True, "diagnostics": {}}
    )
    manifest = payload["diagnostics"]["feed_bundle"]

    assert manifest["total_rows"] == 10
    assert manifest["row_ids"] == [row["id"] for row in rows]
    assert is_complete_feed_bundle_payload(payload) is True

    payload["rows"][5]["items"] = [{"id": "changed-after-manifest"}]
    assert is_complete_feed_bundle_payload(payload) is False


def _radio_card(index: int, *, discovery: bool) -> dict:
    identity = f"artist:{index}"
    return {
        "id": f"radio:{identity}",
        "seed_artist_identity": identity,
        "seed_affinity": "exploratory" if discovery else "direct",
        "thumbnail": f"/artist_artwork/{artist_artwork_token(identity)}",
        "collage_images": [f"/artist_artwork/{artist_artwork_token(identity)}"],
        "tracks": [{"id": f"track:{index}:{track}", "title": f"Work {track}",
                    "artist": f"Artist {index}", "playable": True} for track in range(12)],
    }


def _radio_mix(index: int) -> dict:
    card = _radio_card(index, discovery=False)
    card.update({
        "radio_mode": "multi_artist_mix",
        "seed_artist_identity": f"mix:{index}",
        "seed_artist_identities": [f"artist:{index}", f"artist:{index + 100}"],
        "collage_images": [f"/artist_artwork/{artist_artwork_token(f'artist:{value}')}"
                           for value in (index, index + 100)],
    })
    return card


def _qualified_rotation_card(index: int, *, discovery: bool = False) -> dict:
    identity = f"musicbrainz:artist:{index}"
    token = artist_artwork_token(identity)
    artwork = f"/artist_artwork/{token}"
    tracks = [{"id": f"rotation-track:{index}:{track}", "title": f"Work {track}",
               "artist": f"Artist {index}", "playable": True} for track in range(12)]
    return {
        "id": f"radio:artist:{identity}",
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


def _qualified_rotation_mix(index: int, left: dict, right: dict) -> dict:
    source_ids = [left["seed_artist_identity"], right["seed_artist_identity"]]
    tracks = list(left["tracks"][:6]) + list(right["tracks"][:6])
    return {
        "id": f"radio:mix:{index}",
        "seed_artist_identity": f"mix:{index}",
        "seed_artist_identities": source_ids,
        "seed_affinity": "direct",
        "radio_mode": "multi_artist_mix",
        "thumbnail": left["thumbnail"],
        "collage_images": [left["thumbnail"], right["thumbnail"]],
        "tracks": tracks,
        "items": tracks,
    }


def test_radio_merges_source_metadata_before_admitting_retained_tracks():
    from auralis_backend.discovery.radio_inventory import _merge_radio_tracks
    known = {'id': 'recording:known', 'videoId': '347vCib_lMs', 'title': 'Purple Rain',
             'artist': 'Prince', 'playable': True}
    unknown = {'id': 'youtube:347vCib_lMs', 'title': 'Unknown Track',
               'artist': 'Unknown Artist', 'playable': True}
    unrelated = {'id': 'another-source', 'title': 'Unknown Track', 'artist': 'Unknown Artist', 'playable': True}
    for entries in ([unknown, known, unrelated], [known, unknown, unrelated]):
        tracks = _merge_radio_tracks(entries)
        assert len(tracks) == 1
        assert tracks[0]['title'] == 'Purple Rain'
        assert tracks[0]['artist'] == 'Prince'
    credit_from_channel = {key: value for key, value in known.items() if key != 'artist'}
    credit_from_channel['channel'] = 'Prince'
    assert _merge_radio_tracks([unknown, credit_from_channel])[0]['artist'] == 'Prince'
    second_source = {**known, 'videoId': 'other-source', 'id': 'another-id'}
    assert len(_merge_radio_tracks([known, second_source])) == 1


def test_persisted_radio_publication_rejects_placeholder_or_duplicate_source():
    from copy import deepcopy
    cards = [_radio_mix(0), _radio_mix(1)] + [_radio_card(i + 2, discovery=i < 3) for i in range(10)]
    unknown = deepcopy(cards)
    unknown[0]['tracks'][0]['title'] = 'Unknown Track'
    assert any('metadata_incomplete' in reason for reason in evaluate_popular_radio_contract(unknown).violations)
    duplicate = deepcopy(cards)
    duplicate[0]['tracks'][1]['videoId'] = duplicate[0]['tracks'][0]['id']
    assert any('source_duplicate' in reason for reason in evaluate_popular_radio_contract(duplicate).violations)


@pytest.mark.parametrize('has_repair', [False, True])
def test_radio_index_rewrites_contaminated_global_rows(tmp_path, monkeypatch, has_repair):
    import json
    import sqlite3
    from auralis_backend.discovery import radio_inventory as module
    path = tmp_path / 'radio.sqlite'
    def connect(_server):
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection
    connection = connect(None)
    connection.execute('CREATE TABLE recommendation_feature_store(namespace TEXT, entity_id TEXT, model_id TEXT, payload_json TEXT, updated_at REAL, PRIMARY KEY(namespace, entity_id))')
    seed = 'provider:artist:a'
    unknown = {'id': 'youtube:347vCib_lMs', 'title': 'Unknown Track', 'artist': 'Unknown Artist', 'playable': True}
    payload = {'version': module.RADIO_CATALOG_INDEX_VERSION, 'seed_key': seed, 'cache_identity': seed, 'tracks': [unknown]}
    connection.execute('INSERT INTO recommendation_feature_store VALUES(?,?,?,?,?)',
        [module.RADIO_CATALOG_INDEX_NAMESPACE, f'global:{seed}', 'test', json.dumps(payload), 1])
    connection.commit(); connection.close()
    monkeypatch.setattr(module, 'open_recommendation_store_connection', connect)
    known = {'id': 'recording:known', 'videoId': '347vCib_lMs', 'title': 'Purple Rain', 'artist': 'Prince', 'playable': True}
    repaired = module._load_radio_catalog_index(None, 'account:a', {seed: [known] if has_repair else []})
    assert len(repaired[seed]) == int(has_repair)
    connection = connect(None)
    rows = connection.execute('SELECT payload_json FROM recommendation_feature_store').fetchall()
    connection.close()
    assert len(rows) == 1
    assert json.loads(rows[0]['payload_json'])['tracks'] == repaired[seed]


def test_incomplete_radio_replacement_cannot_copy_the_accepted_parent_row() -> None:
    artists = [
        _qualified_rotation_card(index, discovery=index < 4)
        for index in range(12)
    ]
    accepted_cards = [
        _qualified_rotation_mix(0, artists[0], artists[1]),
        _qualified_rotation_mix(1, artists[2], artists[3]),
        *artists[:10],
    ]
    parent_row = DiscoveryRow(
        id="popular_radio",
        title="Popular Radio",
        kind="popular_radio",
        item_type="radio",
        items=accepted_cards,
    )
    parent = DiscoveryArtifact(
        session_id="parent",
        user_scope_id="user-1",
        profile_key="profile-1",
        generated_at=1,
        expires_at=2,
        rows=[parent_row],
        diagnostics={},
        candidate_pool_counts={},
        provider_timings_ms={},
        home_tab_lanes={},
        accepted=True,
    )
    partial = DiscoveryRow(
        id="popular_radio",
        title="Popular Radio",
        kind="popular_radio",
        item_type="radio",
        items=accepted_cards[:3],
    )

    rows, preserved = _preserve_complete_optional_rows([partial], parent)

    assert preserved == []
    assert rows[0].items == accepted_cards[:3]


def test_feed_contract_has_one_ordered_core_and_explicit_earned_policy() -> None:
    report = evaluate_feed_contract(_core_rows())
    assert report.accepted is True

    missing_core = evaluate_feed_contract(_core_rows()[:-1])
    assert "required_row_missing:quiet_picks" in missing_core.violations

    earned = evaluate_feed_contract(
        _core_rows(),
        earned_row_kinds={"last_played", "because_you_played"},
    )
    assert "required_row_missing:last_played" in earned.violations
    assert "required_row_missing:because_you_played" in earned.violations

    wrong_order = evaluate_feed_contract(list(reversed(_core_rows())))
    assert "row_order_invalid" in wrong_order.violations


@pytest.mark.parametrize("count, expected", [(0, False), (1, False), (7, False), (8, True)])
def test_last_played_is_earned_only_when_builder_minimum_is_met(count, expected) -> None:
    taste = TasteProfile(
        user_scope_id="user-1",
        profile_key="profile-1",
        signal_tier="known",
        last_played_tracks=[{"id": f"track-{index}"} for index in range(count)],
    )

    assert ("last_played" in _earned_row_kinds(taste)) is expected


@pytest.mark.parametrize("count, expected", [(0, False), (7, False), (8, True)])
def test_frequently_listened_earned_threshold_matches_builder(count, expected) -> None:
    taste = TasteProfile(
        user_scope_id="user-1",
        profile_key="profile-1",
        signal_tier="known",
        frequent_tracks=[{"id": f"track-{index}"} for index in range(count)],
    )

    assert ("frequently_listened" in _earned_row_kinds(taste)) is expected


def test_because_you_played_remains_earned_on_any_builder_anchor() -> None:
    taste = TasteProfile(
        user_scope_id="user-1",
        profile_key="profile-1",
        signal_tier="known",
        recent_tracks=[{"id": "one-anchor"}],
    )

    assert "because_you_played" in _earned_row_kinds(taste)


@pytest.mark.parametrize("history_count, expected_accepted", [(7, True), (8, True)])
def test_sparse_last_played_composition_skips_earned_requirement_below_minimum(
    monkeypatch, history_count, expected_accepted
) -> None:
    # Keep the production composer and its final contract check intact while
    # supplying deterministic complete core rows from the row-builder seam.
    rows = [
        DiscoveryRow(
            id=kind,
            title=kind,
            kind=kind,
            item_type="track",
            items=items,
        )
        for kind, items in (
            ("todays_pick", [{"id": f"daily-{index}"} for index in range(6)]),
            ("featured_new_albums", [{"id": f"album-{index}"} for index in range(4)]),
            ("made_for_you", [{"id": f"mix-{index}"} for index in range(5)]),
            ("recommended_artists", [{"id": f"artist-{index}"} for index in range(10)]),
            ("quiet_picks", [{"id": f"quiet-{index}"} for index in range(20)]),
        )
    ]
    if history_count == 8:
        # Eight raw history records can collapse to five distinct playable
        # items after identity and source eligibility checks.
        rows.append(
            DiscoveryRow(
                id="last_played",
                title="last_played",
                kind="last_played",
                item_type="track",
                items=[{"id": f"eligible-{index}"} for index in range(5)],
            )
        )
    monkeypatch.setattr(
        home_composer,
        "build_rows_from_pools",
        lambda *_args: (rows, {}, {}, {}),
    )
    monkeypatch.setattr(
        home_composer,
        "evaluate_quality",
        lambda **_kwargs: (True, [], "accepted"),
    )
    taste = TasteProfile(
        user_scope_id="user-1",
        profile_key="profile-1",
        signal_tier="known",
        last_played_tracks=[
            {"id": f"played-{index % 5}"} for index in range(history_count)
        ],
    )
    candidate = CandidateInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=100,
        expires_at=1000,
        generation_id="candidate-1",
        coverage={"ready": True},
    )

    artifact = home_composer.compose_snapshot_artifact(
        taste=taste,
        candidate=candidate,
        radio=None,
        prior_artifacts=[],
        rotation_epoch=0,
        reason="test",
        now=200,
    )

    assert artifact.accepted is expected_accepted
    assert "last_played" not in [row.kind for row in artifact.rows]
    assert "required_row_missing:last_played" not in artifact.quality_reasons


def test_independent_rows_are_strict_when_present_and_reuse_is_bounded() -> None:
    rows = [
        _row("todays_pick", 6),
        _row("featured_new_albums", 8),
        _row("made_for_you", 8),
        _row("popular_radio", 12),
        _row("recommended_albums", 12),
        _row("recommended_artists", 10),
        _row("quiet_picks", 20),
    ]
    accepted = evaluate_feed_contract(
        rows,
        reused_row_ages_seconds={"popular_radio": 60, "recommended_albums": 60},
    )
    assert accepted.accepted is True
    assert set(accepted.warnings) == {
        "row_reused:popular_radio",
        "row_reused:recommended_albums",
    }

    missing_radio = evaluate_feed_contract(
        [row for row in rows if row["kind"] != "popular_radio"]
    )
    assert missing_radio.accepted is True

    incomplete = list(rows)
    incomplete[3] = _row("popular_radio", 9)
    report = evaluate_feed_contract(incomplete)
    assert "row_below_min:popular_radio:9/10" in report.violations

    expired_reuse = evaluate_feed_contract(
        rows,
        reused_row_ages_seconds={"popular_radio": (8 * 60 * 60) + 1},
    )
    assert "row_reuse_expired:popular_radio" in expired_reuse.violations


def test_popular_radio_contract_owns_quality_discovery_and_rotation_novelty() -> None:
    cards = [_radio_mix(0), _radio_mix(1)] + [_radio_card(index + 2, discovery=index < 3) for index in range(10)]
    assert evaluate_popular_radio_contract(cards).accepted is True

    previous = [f"artist:{index}" for index in range(30, 42)]
    assert evaluate_popular_radio_contract(
        cards, previous_artist_identities=previous
    ).accepted is True

    stale_previous = [f"artist:{index}" for index in range(15)]
    stale = evaluate_popular_radio_contract(
        cards, previous_artist_identities=stale_previous
    )
    assert "radio_rotation_novelty_shortage:2/3" in stale.violations

    weak_discovery = [_radio_mix(0), _radio_mix(1)] + [_radio_card(index + 2, discovery=index < 2) for index in range(10)]
    report = evaluate_popular_radio_contract(weak_discovery)
    assert "radio_discovery_shortage:2/3" in report.violations

    known_discovery = evaluate_popular_radio_contract(
        cards,
        known_artist_identities={"artist:2", "artist:3", "artist:4"},
    )
    assert "radio_user_discovery_shortage:0/3" in known_discovery.violations


def test_actual_radio_rotation_retains_mix_discovery_and_visible_bounds() -> None:
    artist_cards = [
        _qualified_rotation_card(index, discovery=index < 3)
        for index in range(10)
    ]
    cards = [
        *artist_cards,
        _qualified_rotation_mix(0, artist_cards[0], artist_cards[1]),
        _qualified_rotation_mix(1, artist_cards[2], artist_cards[3]),
    ]
    inventory = ArtistRadioInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=100,
        expires_at=1_000,
        generation_id="radio-rotation",
        cards=cards,
        reservoir_cards=cards,
    )

    selected = select_radio_rotation(inventory, epoch=0)

    assert selected is not None
    assert 10 <= len(selected.cards) <= 15
    assert sum(
        card.get("radio_mode") == "multi_artist_mix" for card in selected.cards
    ) >= 2
    assert sum(
        card.get("seed_affinity") in {"related", "exploratory", "similar"}
        for card in selected.cards
    ) >= 3
    assert selected.is_ready is True
    assert evaluate_popular_radio_contract(selected.cards).accepted is True


def test_radio_rotation_avoids_overlapping_mixes_from_old_generations() -> None:
    artists = [_qualified_rotation_card(index, discovery=index < 4) for index in range(12)]
    mixes = [_qualified_rotation_mix(index, artists[left], artists[right])
             for index, (left, right) in enumerate([(0, 1), (0, 2), (1, 2), (3, 4), (0, 4)])]
    cards = [*artists, *mixes]
    inventory = ArtistRadioInventory(user_scope_id="user-1", profile_fingerprint="profile-1",
        generated_at=100, expires_at=1000, generation_id="overlapping-mixes",
        cards=cards, reservoir_cards=cards)
    for epoch in range(4):
        selected = select_radio_rotation(inventory, epoch=epoch)
        assert selected is not None
        assert evaluate_popular_radio_contract(selected.cards).accepted


def test_radio_rotation_orders_newly_verified_cards_before_older_cards() -> None:
    artist_cards = [
        {
            **_qualified_rotation_card(index, discovery=index < 4),
            "first_verified_at": float(100 + index),
            "last_verified_at": float(100 + index),
        }
        for index in range(12)
    ]
    mixes = [
        {
            **_qualified_rotation_mix(0, artist_cards[0], artist_cards[1]),
            "first_verified_at": 90.0,
            "last_verified_at": 90.0,
        },
        {
            **_qualified_rotation_mix(1, artist_cards[2], artist_cards[3]),
            "first_verified_at": 80.0,
            "last_verified_at": 80.0,
        },
    ]
    cards = [*artist_cards, *mixes]
    inventory = ArtistRadioInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=100,
        expires_at=1_000,
        generation_id="newest-first",
        cards=cards,
        reservoir_cards=cards,
    )

    selected = select_radio_rotation(inventory, epoch=0)

    assert selected is not None
    timestamps = [float(card["first_verified_at"]) for card in selected.cards]
    assert timestamps == sorted(timestamps, reverse=True)




def test_radio_rotation_counts_only_unfamiliar_artists_as_discovery() -> None:
    artist_cards = [
        _qualified_rotation_card(index, discovery=index < 6)
        for index in range(20)
    ]
    inventory = ArtistRadioInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=100,
        expires_at=1_000,
        generation_id="radio-profile-aware",
        cards=artist_cards[:12],
        reservoir_cards=artist_cards,
    )

    selected = select_radio_rotation(
        inventory,
        known_artist_identities={
            "musicbrainz:artist:0",
            "musicbrainz:artist:1",
            "musicbrainz:artist:2",
        },
        epoch=0,
    )

    assert selected is not None
    assert selected.diagnostics["reservoir_discovery_card_count"] == 3
    assert selected.diagnostics["selected_discovery_card_count"] == 3
    assert (
        selected.diagnostics["selected_affinity_discovery_card_count"]
        > selected.diagnostics["selected_discovery_card_count"]
    )


def test_radio_rotation_prefers_cards_absent_from_current_feed_even_when_all_were_seen() -> None:
    artist_cards = [
        _qualified_rotation_card(index, discovery=index < 4)
        for index in range(20)
    ]
    cards = [
        *artist_cards,
        _qualified_rotation_mix(0, artist_cards[0], artist_cards[1]),
        _qualified_rotation_mix(1, artist_cards[2], artist_cards[3]),
    ]
    active_ids = {card["id"] for card in cards[:13]}
    inventory = ArtistRadioInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=100,
        expires_at=1_000,
        generation_id="radio-seen-reservoir",
        cards=cards[:13],
        reservoir_cards=cards,
        diagnostics={
            "popular_radio_exposure_ledger": [
                card["seed_artist_identity"] for card in cards
            ]
        },
    )

    selected = select_radio_rotation(
        inventory,
        excluded_card_ids=active_ids,
        comparison_card_ids=active_ids,
        epoch=0,
    )

    assert selected is not None
    selected_ids = {card["id"] for card in selected.cards}
    # Historical exposure no longer qualifies an artist as discovery. The
    # selector still prefers identities absent from the active row and may
    # now replace more than the previous fixed eight-card expectation.
    assert len(selected_ids - active_ids) >= 8
    selected_artists = {card["seed_artist_identity"] for card in selected.cards
                        if card.get("radio_mode") != "multi_artist_mix"}
    assert selected.diagnostics["novel_identity_count"] == len(
        selected_artists - {card["seed_artist_identity"] for card in cards[:13]}
    ) == 7
    assert selected.diagnostics["fresh_rotation"] is True


def _artifact_with_rows(prefix: str, *, unchanged_radio: bool) -> DiscoveryArtifact:
    rows = []
    for kind in ("todays_pick", "made_for_you", "recommended_artists"):
        rows.append(
            DiscoveryRow(
                id=kind,
                title=kind,
                kind=kind,
                item_type="track",
                items=[{"id": f"{prefix}:{kind}:{index}"} for index in range(8)],
            )
        )
    rows.append(
        DiscoveryRow(
            id="popular_radio",
            title="Popular Radio",
            kind="popular_radio",
            item_type="radio",
            items=[
                {
                    "id": f"radio:{index}",
                    "seed_artist_identity": f"artist:{index}",
                }
                for index in range(12)
            ],
        )
    )
    return DiscoveryArtifact(
        session_id=prefix,
        user_scope_id="user-1",
        profile_key="profile-1",
        generated_at=100,
        expires_at=1_000,
        rows=rows,
        diagnostics={},
        candidate_pool_counts={},
        provider_timings_ms={},
        home_tab_lanes={},
        accepted=True,
    )


def test_unchanged_radio_does_not_block_three_other_fresh_rows() -> None:
    previous = _artifact_with_rows("before", unchanged_radio=True)
    candidate = _artifact_with_rows("after", unchanged_radio=True)

    assert _meaningfully_differs(previous, candidate) is True


def test_successor_novelty_measures_only_the_initial_visible_page() -> None:
    previous = DiscoveryArtifact(
        session_id="before",
        user_scope_id="user-1",
        profile_key="profile-1",
        generated_at=100,
        expires_at=1_000,
        rows=[
            DiscoveryRow(
                id="quiet_picks",
                title="Quiet Picks",
                kind="quiet_picks",
                item_type="track",
                items=[{"id": f"same-{index}"} for index in range(20)]
                + [{"id": f"old-reserve-{index}"} for index in range(20)],
            )
        ],
        diagnostics={},
        candidate_pool_counts={},
        provider_timings_ms={},
        home_tab_lanes={},
        accepted=True,
    )
    candidate = DiscoveryArtifact(
        **{
            **previous.__dict__,
            "session_id": "after",
            "rows": [
                DiscoveryRow(
                    id="quiet_picks",
                    title="Quiet Picks",
                    kind="quiet_picks",
                    item_type="track",
                    items=[{"id": f"same-{index}"} for index in range(20)]
                    + [{"id": f"new-reserve-{index}"} for index in range(20)],
                )
            ],
        }
    )

    novelty = _successor_novelty(previous, candidate)

    assert novelty["accepted"] is False
    assert novelty["new_visible_identities"] == 0


def test_small_changes_cannot_promote_a_mostly_recycled_home():
    previous = _artifact_with_rows('before', unchanged_radio=True)
    from copy import deepcopy
    candidate = deepcopy(previous)
    for row in candidate.rows:
        if row.kind in {'recommended_albums', 'recommended_artists'}:
            for index, item in enumerate(row.items):
                if index < 2:
                    item['id'] = f'new-{row.kind}-{index}'
    assert not _successor_novelty(previous, candidate)['accepted']


def test_mix_freshness_measures_recordings_not_permanent_card_id():
    from copy import deepcopy
    previous = _artifact_with_rows('before', unchanged_radio=True)
    previous.rows = [DiscoveryRow(id='made_for_you', title='Mixes', kind='made_for_you',
        item_type='mix', items=[{'id':'daily_mix_1', 'tracks':[{'id':f'old-{i}'} for i in range(8)]}])]
    candidate = deepcopy(previous)
    candidate.rows[0].items[0]['tracks'] = [{'id':f'new-{i}'} for i in range(8)]
    assert _successor_novelty(previous, candidate)['accepted']
    candidate.rows[0].items[0]['tracks'] = [{'id':f'old-{i}'} for i in range(8)]
    candidate.rows[0].items[0]['id'] = 'renamed_mix'
    assert not _successor_novelty(previous, candidate)['accepted']


def test_snapshot_and_artifact_ttls_have_distinct_generation_and_serving_rules() -> None:
    snapshot = CandidateSnapshotRecord(
        snapshot_id="candidate-1",
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=100,
        expires_at=100 + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        payload={"tracks": []},
    )
    assert snapshot.is_fresh(101) is True
    assert snapshot.is_fresh(snapshot.expires_at) is False
    assert snapshot.is_usable(snapshot.expires_at) is True

    with pytest.raises(ValueError, match="eight-hour TTL"):
        CandidateSnapshotRecord(
            snapshot_id="candidate-too-long",
            user_scope_id="user-1",
            profile_fingerprint="profile-1",
            generated_at=100,
            expires_at=101 + CANDIDATE_SNAPSHOT_TTL_SECONDS,
            payload={},
        )

    artifact = FeedArtifactRecord(
        artifact_id="feed-1",
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        candidate_snapshot_id="candidate-1",
        radio_snapshot_id="radio-1",
        created_at=100,
        expires_at=100 + ACTIVE_ARTIFACT_TTL_SECONDS,
        payload={"rows": _core_rows()},
    )
    assert artifact.is_promotable(100 + READY_ARTIFACT_TTL_SECONDS) is True
    assert artifact.is_promotable(101 + READY_ARTIFACT_TTL_SECONDS) is False
    assert artifact.is_servable(101 + READY_ARTIFACT_TTL_SECONDS) is True


def test_all_home_jobs_share_one_bounded_backoff_contract() -> None:
    assert [retry_delay_seconds(value) for value in range(1, 8)] == [
        5,
        10,
        20,
        40,
        80,
        160,
        300,
    ]
    assert retry_delay_seconds(20) == 300
