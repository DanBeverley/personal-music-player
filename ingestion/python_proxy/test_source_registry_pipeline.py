from __future__ import annotations

import threading
from collections import Counter
from unittest.mock import patch

from auralis_backend.discovery.enrichment import MaterializedCandidateSupply
from auralis_backend.discovery.schema import TasteProfile
from auralis_backend.discovery.source_registry import (
    _load_sources,
    _match_source,
    _shortlist,
    _store_sources,
    verify_materialized_supply,
)
from auralis_backend.discovery.structured_providers import CanonicalRecording


class _Server:
    pass


def _taste() -> TasteProfile:
    return TasteProfile(
        user_scope_id="source-registry-test",
        profile_key="source-registry-profile",
        signal_tier="established",
    )


def _track(index: int) -> dict:
    return {
        "title": f"Canonical Track {index}",
        "artist": f"Canonical Artist {index}",
        "duration": 220,
        "musicbrainz_recording_id": f"00000000-0000-4000-8000-{index:012d}",
        "materialized_relation": "track_radio",
        "relationship_score": 0.9 - index * 0.001,
    }


def _entity_key(index: int) -> str:
    return f"musicbrainz:recording:00000000-0000-4000-8000-{index:012d}"


def _source(index: int, *, state: str = "verified") -> dict:
    return {
        "provider": "youtube",
        "source_id": f"Y{index:010d}"[-11:],
        "authority": "topic",
        "identity_confidence": 0.96,
        "verification_state": state,
        "verified_at": 9999999999.0,
    }


def test_verified_registry_hit_is_reused_without_provider_or_stream_calls() -> None:
    supply = MaterializedCandidateSupply(pools={"similarity": [_track(1)]})
    with (
        patch(
            "auralis_backend.discovery.source_registry._load_sources",
            return_value={_entity_key(1): [_source(1)]},
        ),
        patch(
            "auralis_backend.discovery.source_registry._provider_is_blocked",
            return_value=False,
        ),
        patch("auralis_backend.discovery.source_registry._enumerate_youtube_sources") as enumerate_sources,
        patch("auralis_backend.discovery.source_registry._verify_source") as verify_source,
    ):
        resolved = verify_materialized_supply(_Server(), supply, _taste())

    assert resolved.pools["similarity"][0]["playable"] is True
    assert resolved.pools["similarity"][0]["videoId"] == _source(1)["source_id"]
    assert resolved.diagnostics["source_registry_hits"] == 1
    enumerate_sources.assert_not_called()
    verify_source.assert_not_called()


def test_radio_artist_artwork_is_prioritized_ahead_of_general_tracks() -> None:
    general_tracks = [_track(index) for index in range(1, 61)]
    radio_track = {
        **_track(99),
        "artist": "Priority Radio Artist",
        "channel": "Priority Radio Artist - Topic",
        "radio_seed_artist": "Priority Radio Artist",
    }
    all_tracks = [*general_tracks, radio_track]
    stored = {}
    for index, track in enumerate(all_tracks, start=1):
        entity_key = (
            f"musicbrainz:recording:{track['musicbrainz_recording_id']}"
        )
        stored[entity_key] = [
            {
                **_source(index),
                "artist_browse_ids": [f"UC{index:022d}"],
                "catalog_artist": track["artist"],
                "channel_id": f"UC{index:022d}",
            }
        ]
    radio_artist_id = "UC0000000000000000000061"

    def channel_artwork(_server, channel_ids):
        ordered = list(dict.fromkeys(channel_ids))
        assert ordered[0] == radio_artist_id
        return {radio_artist_id: "https://example.test/radio-artist.jpg"}

    supply = MaterializedCandidateSupply(
        pools={
            "similarity": general_tracks,
            "radio_artist_catalog": [radio_track],
        }
    )
    with (
        patch(
            "auralis_backend.discovery.source_registry._load_sources",
            return_value=stored,
        ),
        patch(
            "auralis_backend.discovery.source_registry._provider_is_blocked",
            return_value=False,
        ),
        patch(
            "auralis_backend.discovery.source_registry._youtube_channel_thumbnails",
            side_effect=channel_artwork,
        ),
        patch("auralis_backend.discovery.source_registry._store_sources"),
    ):
        resolved = verify_materialized_supply(_Server(), supply, _taste())

    assert resolved.pools["radio_artist_catalog"][0]["artist_thumbnail"] == (
        "https://example.test/radio-artist.jpg"
    )


def test_source_identity_match_accepts_official_decoration_but_rejects_wrong_artist() -> None:
    recording = CanonicalRecording(
        title="Canonical Track",
        artist="Canonical Artist",
        duration_seconds=220,
    )
    accepted = _match_source(
        recording,
        {
            "source_id": "Y0000000001",
            "title": "Canonical Track (Official Audio)",
            "channel": "Canonical Artist - Topic",
            "duration": 220,
        },
    )
    rejected = _match_source(
        recording,
        {
            "source_id": "Y0000000002",
            "title": "Canonical Track",
            "channel": "Unrelated Cover Channel",
            "duration": 220,
        },
    )

    assert accepted is not None
    assert accepted["authority"] == "topic"
    assert rejected is None


def test_ytmusic_catalog_artist_can_validate_a_label_channel() -> None:
    recording = CanonicalRecording(
        title="Canonical Track",
        artist="Canonical Artist",
        duration_seconds=220,
    )

    accepted = _match_source(
        recording,
        {
            "source_id": "Y0000000003",
            "title": "Canonical Track",
            "channel": "Example Records",
            "channel_id": "UC0000000000000000000000",
            "catalog_artist": "Canonical Artist",
            "album": "Canonical Album",
            "album_id": "MPREb_example",
            "ytmusic_catalog": True,
            "duration": 220,
        },
    )

    assert accepted is not None
    assert accepted["authority"] == "verified_catalog"
    assert accepted["artist_match_score"] >= 0.96


def test_unofficial_exact_match_is_not_promoted_to_trusted_authority() -> None:
    recording = CanonicalRecording(
        title="Canonical Track",
        artist="Canonical Artist",
        duration_seconds=220,
    )

    accepted = _match_source(
        recording,
        {
            "source_id": "Y0000000004",
            "title": "Canonical Track",
            "channel": "Canonical Artist Archive",
            "channel_id": "UC0000000000000000000001",
            "duration": 220,
        },
    )

    assert accepted is not None
    assert accepted["authority"] == "user_upload"


def test_shortlist_rejects_known_bad_candidates_before_provider_work() -> None:
    good = _track(1)
    search_only = {**_track(2), "source_authority": "search_only"}
    hidden = {**_track(3), "negative_feedback_state": "hidden"}
    rejections = {}

    shortlisted = _shortlist(
        MaterializedCandidateSupply(
            pools={"similarity": [search_only, hidden, good]}
        ),
        _taste(),
        {},
        limit=3,
        rejection_counts=rejections,
    )

    assert [key for _pool, key, _item in shortlisted] == [_entity_key(1)]
    assert rejections == {"search_only": 1, "negative_feedback": 1}


def test_shortlist_reserves_capacity_for_radio_and_complete_albums() -> None:
    radio = []
    for index in range(20):
        item = _track(index)
        item["radio_seed_artist"] = "Canonical Artist 0"
        item["related_to_artist"] = "Canonical Artist 0"
        radio.append(item)
    albums = [
        {
            "id": "album-1",
            "canonical_tracks": [_track(100 + index) for index in range(20)],
        }
    ]
    supply = MaterializedCandidateSupply(
        pools={
            "similarity": [_track(200 + index) for index in range(20)],
            "radio_artist_catalog": radio,
            "album": albums,
        }
    )

    shortlisted = _shortlist(supply, _taste(), {}, limit=32)
    counts = Counter(pool for pool, _key, _item in shortlisted)

    assert counts["radio_artist_catalog"] == 12
    assert counts["album"] == 12
    assert counts["similarity"] == 8


def test_saved_alternatives_try_authoritative_source_first() -> None:
    supply = MaterializedCandidateSupply(pools={"similarity": [_track(1)]})
    unofficial = {**_source(1, state="pending"), "authority": "user_upload"}
    official = {
        **_source(2, state="pending"),
        "authority": "official_artist_channel",
        "identity_confidence": 0.9,
    }

    def verify_source(_server, _entity_key_value, source):
        return {
            **source,
            "verification_state": "verified",
            "verified_at": 9999999999.0,
        }, "verified_resolver"

    with (
        patch(
            "auralis_backend.discovery.source_registry._load_sources",
            return_value={_entity_key(1): [unofficial, official]},
        ),
        patch("auralis_backend.discovery.source_registry._provider_is_blocked", return_value=False),
        patch(
            "auralis_backend.discovery.source_registry._verify_source",
            side_effect=verify_source,
        ) as verifier,
        patch("auralis_backend.discovery.source_registry._store_sources"),
        patch("auralis_backend.discovery.source_registry._set_provider_health"),
    ):
        resolved = verify_materialized_supply(
            _Server(),
            supply,
            _taste(),
            max_new_verifications=1,
            max_workers=1,
        )

    assert verifier.call_args.args[2]["source_id"] == official["source_id"]
    assert resolved.pools["similarity"][0]["source_identity_authority"] == "official_artist_channel"


def test_only_the_bounded_shortlist_is_verified() -> None:
    supply = MaterializedCandidateSupply(
        pools={"similarity": [_track(index) for index in range(10)]}
    )

    def enumerate_source(_server, item):
        index = int(str(item["musicbrainz_recording_id"]).split("-")[-1])
        return [_source(index, state="pending")]

    def verify_source(_server, _entity_key_value, source):
        return {**source, "verification_state": "verified", "verified_at": 9999999999.0}, "verified_resolver"

    with (
        patch("auralis_backend.discovery.source_registry._load_sources", return_value={}),
        patch("auralis_backend.discovery.source_registry._provider_is_blocked", return_value=False),
        patch("auralis_backend.discovery.source_registry._enumerate_youtube_sources", side_effect=enumerate_source),
        patch("auralis_backend.discovery.source_registry._verify_source", side_effect=verify_source) as verifier,
        patch("auralis_backend.discovery.source_registry._store_sources"),
        patch("auralis_backend.discovery.source_registry._set_provider_health"),
    ):
        resolved = verify_materialized_supply(
            _Server(),
            supply,
            _taste(),
            max_new_verifications=3,
            max_workers=2,
        )

    assert verifier.call_count == 3
    assert resolved.diagnostics["source_verification_attempted"] == 3
    assert len(resolved.pools["similarity"]) == 3


def test_adaptive_scheduler_verifies_more_than_sixteen_across_pools() -> None:
    supply = MaterializedCandidateSupply(
        pools={
            "similarity": [_track(index) for index in range(0, 20)],
            "artist_graph": [_track(index) for index in range(100, 120)],
            "profile_spine": [_track(index) for index in range(200, 220)],
            "genre_mood": [_track(index) for index in range(300, 320)],
        }
    )

    def enumerate_source(_server, item):
        index = int(str(item["musicbrainz_recording_id"]).split("-")[-1])
        return [_source(index, state="pending")]

    def verify_source(_server, _entity_key_value, source):
        return {
            **source,
            "verification_state": "verified",
            "verified_at": 9999999999.0,
        }, "verified_resolver"

    with (
        patch("auralis_backend.discovery.source_registry._load_sources", return_value={}),
        patch("auralis_backend.discovery.source_registry._provider_is_blocked", return_value=False),
        patch(
            "auralis_backend.discovery.source_registry._enumerate_youtube_sources",
            side_effect=enumerate_source,
        ),
        patch(
            "auralis_backend.discovery.source_registry._verify_source",
            side_effect=verify_source,
        ) as verifier,
        patch("auralis_backend.discovery.source_registry._store_sources"),
        patch("auralis_backend.discovery.source_registry._set_provider_health"),
    ):
        resolved = verify_materialized_supply(
            _Server(),
            supply,
            _taste(),
        )

    assert resolved.diagnostics["source_verification_limit"] == 32
    assert resolved.diagnostics["source_verification_attempted"] == 32
    assert resolved.diagnostics["source_verification_attempted_by_pool"] == {
        "similarity": 8,
        "artist_graph": 8,
        "profile_spine": 8,
        "genre_mood": 8,
    }
    assert verifier.call_count == 32


def test_source_lookup_misses_are_replaced_in_the_same_cycle() -> None:
    supply = MaterializedCandidateSupply(
        pools={"similarity": [_track(index) for index in range(12)]}
    )

    def enumerate_source(_server, item):
        index = int(str(item["musicbrainz_recording_id"]).split("-")[-1])
        if index < 2:
            return []
        return [_source(index, state="pending")]

    def verify_source(_server, _entity_key_value, source):
        return {
            **source,
            "verification_state": "verified",
            "verified_at": 9999999999.0,
        }, "verified_resolver"

    with (
        patch("auralis_backend.discovery.source_registry._load_sources", return_value={}),
        patch("auralis_backend.discovery.source_registry._provider_is_blocked", return_value=False),
        patch(
            "auralis_backend.discovery.source_registry._enumerate_youtube_sources",
            side_effect=enumerate_source,
        ),
        patch(
            "auralis_backend.discovery.source_registry._verify_source",
            side_effect=verify_source,
        ) as verifier,
        patch("auralis_backend.discovery.source_registry._store_sources"),
        patch("auralis_backend.discovery.source_registry._set_provider_health"),
    ):
        resolved = verify_materialized_supply(
            _Server(),
            supply,
            _taste(),
            max_new_verifications=4,
            max_workers=2,
        )

    assert resolved.diagnostics["source_lookup_misses"] == 2
    assert resolved.diagnostics["source_verification_attempted"] == 4
    assert resolved.diagnostics["source_verification_verified"] == 4
    assert verifier.call_count == 4


def test_recent_empty_lookup_is_not_requested_again() -> None:
    supply = MaterializedCandidateSupply(pools={"similarity": [_track(1)]})
    marker = {
        "provider": "youtube_lookup",
        "source_id": "exact_recording",
        "verification_state": "no_match",
        "retry_at": 9999999999.0,
    }
    with (
        patch(
            "auralis_backend.discovery.source_registry._load_sources",
            return_value={_entity_key(1): [marker]},
        ),
        patch(
            "auralis_backend.discovery.source_registry._provider_is_blocked",
            return_value=False,
        ),
        patch(
            "auralis_backend.discovery.source_registry._enumerate_youtube_sources"
        ) as enumerate_sources,
        patch("auralis_backend.discovery.source_registry._verify_source") as verify_source,
    ):
        resolved = verify_materialized_supply(
            _Server(),
            supply,
            _taste(),
            max_new_verifications=1,
        )

    assert resolved.diagnostics["source_lookup_deferred"] == 1
    assert resolved.diagnostics["source_verification_attempted"] == 0
    enumerate_sources.assert_not_called()
    verify_source.assert_not_called()


def test_all_exact_source_alternatives_are_saved_under_one_recording() -> None:
    supply = MaterializedCandidateSupply(pools={"similarity": [_track(4)]})
    alternatives = [
        {**_source(index, state="pending"), "source_id": f"A{index:010d}"[-11:]}
        for index in range(3)
    ]
    persisted = []

    def store_sources(_server, sources, **_kwargs):
        rows = list(sources)
        persisted.extend(rows)
        return len(rows)

    def verify_source(_server, _entity_key_value, source):
        return {
            **source,
            "verification_state": "verified",
            "verified_at": 9999999999.0,
        }, "verified_resolver"

    with (
        patch("auralis_backend.discovery.source_registry._load_sources", return_value={}),
        patch("auralis_backend.discovery.source_registry._provider_is_blocked", return_value=False),
        patch(
            "auralis_backend.discovery.source_registry._enumerate_youtube_sources",
            return_value=alternatives,
        ),
        patch(
            "auralis_backend.discovery.source_registry._verify_source",
            side_effect=verify_source,
        ),
        patch(
            "auralis_backend.discovery.source_registry._store_sources",
            side_effect=store_sources,
        ),
        patch("auralis_backend.discovery.source_registry._set_provider_health"),
    ):
        resolved = verify_materialized_supply(
            _Server(),
            supply,
            _taste(),
            max_new_verifications=1,
            max_workers=1,
        )

    persisted_sources = [
        (entity_key, source)
        for entity_key, source in persisted
        if source.get("provider") == "youtube"
    ]
    assert {source["source_id"] for _key, source in persisted_sources} == {
        source["source_id"] for source in alternatives
    }
    assert {entity_key for entity_key, _source_value in persisted_sources} == {
        _entity_key(4)
    }
    assert resolved.diagnostics["source_registry_candidates"] == 1
    assert len(resolved.pools["similarity"]) == 1


def test_two_bot_challenges_open_the_circuit_and_stop_remaining_work() -> None:
    server = _Server()
    supply = MaterializedCandidateSupply(
        pools={"similarity": [_track(index) for index in range(8)]}
    )

    def enumerate_source(_server, item):
        index = int(str(item["musicbrainz_recording_id"]).split("-")[-1])
        return [_source(index, state="pending")]

    with (
        patch("auralis_backend.discovery.source_registry._load_sources", return_value={}),
        patch("auralis_backend.discovery.source_registry._provider_is_blocked", return_value=False),
        patch("auralis_backend.discovery.source_registry._enumerate_youtube_sources", side_effect=enumerate_source),
        patch("auralis_backend.discovery.source_registry._verify_source", return_value=(None, "source_blocked")) as verifier,
        patch("auralis_backend.discovery.source_registry._store_sources"),
        patch("auralis_backend.discovery.source_registry._set_provider_health") as set_health,
    ):
        resolved = verify_materialized_supply(
            server,
            supply,
            _taste(),
            max_new_verifications=8,
            max_workers=2,
        )

    assert verifier.call_count == 2
    assert resolved.diagnostics["source_verification_blocked"] == 2
    set_health.assert_called_once_with(server, blocked=True, failures=2)


def test_album_qualifies_only_from_its_verified_canonical_tracklist() -> None:
    tracks = [_track(index) for index in range(10)]
    stored = {
        _entity_key(index): [_source(index)]
        for index in range(8)
    }
    supply = MaterializedCandidateSupply(
        pools={
            "album": [
                {
                    "id": "musicbrainz:release-group:album-1",
                    "title": "Canonical Album",
                    "artist": "Canonical Artist",
                    "canonical_tracks": tracks,
                }
            ]
        }
    )
    with (
        patch("auralis_backend.discovery.source_registry._load_sources", return_value=stored),
        patch("auralis_backend.discovery.source_registry._provider_is_blocked", return_value=True),
    ):
        resolved = verify_materialized_supply(_Server(), supply, _taste())

    album = resolved.pools["album"][0]
    assert album["playable"] is True
    assert album["track_count"] == 8
    assert album["canonical_track_count"] == 10
    assert album["playable_coverage"] == 0.8


def test_source_batch_uses_one_initialized_connection_and_one_transaction() -> None:
    connection = type(
        "Connection",
        (),
        {
            "execute": lambda self, statement, values: type(
                "Cursor", (), {"fetchall": lambda self: []}
            )(),
            "executemany": lambda self, statement, rows: setattr(self, "rows", list(rows)),
            "commit": lambda self: setattr(self, "committed", True),
            "close": lambda self: setattr(self, "closed", True),
        },
    )()
    with (
        patch(
            "auralis_backend.discovery.source_registry.open_recommendation_store_connection_without_init",
            return_value=connection,
        ) as open_without_init,
        patch(
            "auralis_backend.discovery.source_registry.open_recommendation_store_connection"
        ) as open_with_init,
    ):
        stored = _store_sources(
            _Server(),
            [
                (_entity_key(1), _source(1)),
                (_entity_key(2), _source(2)),
            ],
            store_initialized=True,
        )

    assert stored == 2
    assert len(connection.rows) == 2
    assert connection.committed is True
    assert connection.closed is True
    open_without_init.assert_called_once()
    open_with_init.assert_not_called()


def test_verified_registry_source_is_the_same_source_used_by_playback(tmp_path) -> None:
    from auralis_backend.api.stream_runtime import playback_source_candidates

    server = _Server()
    server.RECOMMENDATION_STORE_DB_PATH = str(tmp_path / "recommendation.sqlite3")
    server.recommendation_store_lock = threading.RLock()
    entity_key = _entity_key(7)
    _store_sources(
        server,
        [
            (entity_key, _source(7)),
            (
                entity_key,
                {
                    **_source(8),
                    "verification_state": "unavailable",
                    "failure_reason": "video_unavailable",
                },
            ),
        ],
    )

    sources = playback_source_candidates(
        server,
        "recording:00000000-0000-4000-8000-000000000007",
    )

    assert [source["source_id"] for source in sources] == [_source(7)["source_id"]]
    assert sources[0]["verification_state"] == "verified"


def test_playback_source_batch_reads_multiple_recordings_once(tmp_path) -> None:
    from auralis_backend.api.stream_runtime import playback_source_candidates_batch
    from auralis_backend.recommend import store_runtime

    server = _Server()
    server.RECOMMENDATION_STORE_DB_PATH = str(tmp_path / "recommendation.sqlite3")
    server.recommendation_store_lock = threading.RLock()
    first_key = _entity_key(17)
    second_key = _entity_key(18)
    _store_sources(
        server,
        [
            (first_key, _source(17)),
            (second_key, _source(18)),
        ],
    )

    with patch.object(
        store_runtime,
        "open_recommendation_store_connection",
        wraps=store_runtime.open_recommendation_store_connection,
    ) as open_store:
        sources = playback_source_candidates_batch(
            server,
            [
                "recording:00000000-0000-4000-8000-000000000017",
                "recording:00000000-0000-4000-8000-000000000018",
            ],
        )

    assert open_store.call_count == 1
    assert [source["source_id"] for source in sources[
        "recording:00000000-0000-4000-8000-000000000017"
    ]] == [_source(17)["source_id"]]
    assert [source["source_id"] for source in sources[
        "recording:00000000-0000-4000-8000-000000000018"
    ]] == [_source(18)["source_id"]]


def test_legacy_playback_accepts_only_real_youtube_video_ids() -> None:
    from auralis_backend.api.stream_runtime import playback_source_candidates_batch

    sources = playback_source_candidates_batch(
        _Server(),
        [
            "musicbrainz",
            "musicbrainz:release:metadata-only",
            "Flight of Icarus",
            "abcdefghijk",
        ],
    )

    assert sources["musicbrainz"] == []
    assert sources["musicbrainz:release:metadata-only"] == []
    assert sources["Flight of Icarus"] == []
    assert sources["abcdefghijk"] == [
        {
            "provider": "youtube",
            "source_id": "abcdefghijk",
            "authority": "legacy",
        }
    ]


def test_unavailable_registry_source_is_not_reset_by_rediscovery(tmp_path) -> None:
    server = _Server()
    server.RECOMMENDATION_STORE_DB_PATH = str(tmp_path / "recommendation.sqlite3")
    server.recommendation_store_lock = threading.RLock()
    entity_key = _entity_key(9)
    unavailable = {
        **_source(9),
        "verification_state": "unavailable",
        "failure_reason": "video_unavailable",
    }

    _store_sources(server, [(entity_key, unavailable)])
    stored = _store_sources(server, [(entity_key, _source(9, state="pending"))])
    sources = _load_sources(server, [entity_key])[entity_key]

    assert stored == 0
    assert len(sources) == 1
    assert sources[0]["verification_state"] == "unavailable"
    assert sources[0]["failure_reason"] == "video_unavailable"
