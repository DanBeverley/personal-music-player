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
    merge_radio_reservoirs,
    select_radio_rotation,
)
from auralis_backend.discovery.inventory import (  # noqa: E402
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
    _meaningfully_differs,
    _successor_novelty,
    _preserve_complete_optional_rows,
    artifact_exposure_entries,
)
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
        "thumbnail": f"/artist_artwork/{index}",
        "collage_images": [f"/artist_artwork/{index}"],
        "tracks": [{"id": f"track:{index}:{track}"} for track in range(12)],
    }


def _radio_mix(index: int) -> dict:
    card = _radio_card(index, discovery=False)
    card.update({
        "radio_mode": "multi_artist_mix",
        "seed_artist_identity": f"mix:{index}",
        "seed_artist_identities": [f"artist:{index}", f"artist:{index + 100}"],
        "collage_images": [f"/artist_artwork/{index}", f"/artist_artwork/{index + 100}"],
    })
    return card


def _qualified_rotation_card(index: int, *, discovery: bool = False) -> dict:
    identity = f"musicbrainz:artist:{index}"
    token = artist_artwork_token(identity)
    artwork = f"/artist_artwork/{token}"
    tracks = [{"id": f"rotation-track:{index}:{track}"} for track in range(12)]
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


def test_radio_reservoir_merge_preserves_original_first_verified_time() -> None:
    existing_card = {
        **_qualified_rotation_card(1, discovery=True),
        "first_verified_at": 10.0,
        "last_verified_at": 10.0,
    }
    incoming_card = {
        **existing_card,
        "first_verified_at": 20.0,
        "last_verified_at": 20.0,
    }
    existing = ArtistRadioInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=10,
        expires_at=1_000,
        generation_id="existing",
        cards=[existing_card],
        reservoir_cards=[existing_card],
    )
    incoming = ArtistRadioInventory(
        user_scope_id="user-1",
        profile_fingerprint="profile-1",
        generated_at=20,
        expires_at=1_000,
        generation_id="incoming",
        cards=[incoming_card],
        reservoir_cards=[incoming_card],
    )

    merged = merge_radio_reservoirs(existing, incoming)

    assert merged.reservoir_cards[0]["first_verified_at"] == 10.0
    assert merged.reservoir_cards[0]["last_verified_at"] == 20.0


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
    assert selected.diagnostics["novel_identity_count"] >= 8
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
