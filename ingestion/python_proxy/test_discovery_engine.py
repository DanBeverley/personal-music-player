from __future__ import annotations

import time
from datetime import datetime, timezone

from auralis_backend.discovery.adapters import home_response_from_artifact, row_page_response_from_artifact
from auralis_backend.discovery.artifact import _artifact_from_dict, artifact_to_dict, evaluate_quality
from auralis_backend.discovery.config import DISCOVERY_UNIVERSE_TARGET, ROW_RECIPES, TRACK_POOL_QUOTAS
from auralis_backend.discovery.candidates import (
    _add_candidate,
    _balanced_track_universe,
    _album_item_from_track,
    _album_segment_for_candidate,
    _album_segment_pools,
    _genre_mood_pool,
    artist_name,
    normalize_track,
)
from auralis_backend.discovery.ranking import (
    build_home_lanes,
    build_personal_mixes,
    build_trending_genre_tabs,
    rank_albums,
    rank_tracks,
)
from auralis_backend.discovery.schema import DiscoveryArtifact, DiscoveryCandidate, DiscoveryRow, TasteProfile
from auralis_backend.discovery.service import DiscoveryService


def _taste() -> TasteProfile:
    return TasteProfile(
        user_scope_id="user-a",
        profile_key="profile-a",
        signal_tier="known",
        recent_tracks=[
            {"id": "seed-rock", "title": "Thunder Rock", "artist": "Band A", "genre": "rock"},
            {"id": "seed-pop", "title": "Bright Pop", "artist": "Band B", "genre": "pop"},
        ],
        top_tracks=[
            {"id": "top-rock", "title": "Classic Rock Hit", "artist": "Band C", "genre": "rock"},
        ],
        artist_hints=["Band A", "Band C"],
    )


def _track(item_id: str, title: str, artist: str, text: str, source: str = "genre_mood") -> DiscoveryCandidate:
    return DiscoveryCandidate(
        item={
            "id": item_id,
            "title": title,
            "artist": artist,
            "genre": text,
            "album": f"{artist} Album",
        },
        source=source,
        score=2.0,
    )


def _album(item_id: str, title: str, artist: str, text: str, source_track_id: str) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        item={
            "id": item_id,
            "title": title,
            "artist": artist,
            "genre": text,
            "source_track_id": source_track_id,
        },
        source="album",
        score=2.0,
        item_type="album",
    )


class _FeatureServer:
    @staticmethod
    def _recommendation_trim_text(value) -> str:
        return str(value or "").strip()

    @staticmethod
    def _normalize_text(value) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @staticmethod
    def normalize_recommendation_track(raw):
        return {
            "id": raw.get("id"),
            "title": raw.get("title"),
            "artist": raw.get("artist"),
            "channel": raw.get("artist"),
            "album": raw.get("album"),
        }


class _LaneSearchServer(_FeatureServer):
    @staticmethod
    def _assistant_tool_search_tracks(query, _limit):
        query_id = "-".join(str(query).lower().split())
        return [
            {
                "id": query_id,
                "title": str(query),
                "artist": f"Artist for {query} - Topic",
            }
        ]


def test_quality_rejects_two_row_feed() -> None:
    rows = [
        DiscoveryRow("last_played", "Last played", "last_played", "track", [{"id": "a"}]),
        DiscoveryRow("frequently_listened", "Frequently listened", "frequently_listened", "track", [{"id": "b"}]),
    ]
    accepted, reasons, status = evaluate_quality(
        rows=rows,
        taste=_taste(),
        home_tab_diagnostics={"accepted": True, "lane_item_counts": {"all": 12, "chill": 12, "workout": 12, "focus": 12, "mood": 12}},
    )
    assert not accepted
    assert status == "rejected"
    assert "weak_two_row_feed" in reasons


def test_quality_marks_feed_without_trending_by_genre_launchable() -> None:
    rows = [
        DiscoveryRow("todays_pick", "Today's pick", "todays_pick", "track", [{"id": "today"}]),
        DiscoveryRow("featured_new_albums", "Featured albums", "featured_new_albums", "album", [{"id": "album"}]),
        DiscoveryRow("last_played", "Last played", "last_played", "track", [{"id": "last"}]),
        DiscoveryRow("frequently_listened", "Frequently listened", "frequently_listened", "track", [{"id": "freq"}]),
        DiscoveryRow("made_for_you", "Made for you", "made_for_you", "track", [{"id": "mix"}]),
        DiscoveryRow("because_you_played", "Because you played", "because_you_played", "track", [{"id": "because"}]),
        DiscoveryRow("recommended_albums", "Recommended albums", "recommended_albums", "album", [{"id": "album-2"}]),
        DiscoveryRow("recommended_artists", "Recommended artists", "recommended_artists", "artist", [{"id": "artist"}]),
        DiscoveryRow("quiet_picks", "Quiet Picks", "quiet_picks", "track", [{"id": "quiet"}]),
        DiscoveryRow("hidden_gems", "Hidden gems", "hidden_gems", "track", [{"id": "hidden"}]),
    ]
    accepted, reasons, status = evaluate_quality(
        rows=rows,
        taste=_taste(),
        home_tab_diagnostics={
            "accepted": True,
            "lane_item_counts": {"all": 12, "chill": 12, "workout": 12, "focus": 12, "mood": 12},
        },
    )
    assert accepted is True
    assert status == "launchable"
    assert "missing_trending_by_genre" in reasons


def test_quality_accepts_strong_feed_with_partial_usable_lanes() -> None:
    rows = [
        DiscoveryRow("todays_pick", "Today's pick", "todays_pick", "track", [{"id": "today"}]),
        DiscoveryRow("featured_new_albums", "Featured albums", "featured_new_albums", "album", [{"id": "album"}]),
        DiscoveryRow("last_played", "Last played", "last_played", "track", [{"id": "last"}]),
        DiscoveryRow("frequently_listened", "Frequently listened", "frequently_listened", "track", [{"id": "freq"}]),
        DiscoveryRow("made_for_you", "Made for you", "made_for_you", "track", [{"id": "mix"}]),
        DiscoveryRow("because_you_played", "Because you played", "because_you_played", "track", [{"id": "because"}]),
        DiscoveryRow(
            "trending_by_genre",
            "Trending by genre",
            "trending_by_genre",
            "track",
            [{"id": "genre-a"}, {"id": "genre-b"}],
            meta={"tabs": [{"id": "rock", "tracks": [{"id": "genre-a"}]}, {"id": "soul", "tracks": [{"id": "genre-b"}]}]},
        ),
        DiscoveryRow("recommended_albums", "Recommended albums", "recommended_albums", "album", [{"id": "album-2"}]),
        DiscoveryRow("quiet_picks", "Quiet Picks", "quiet_picks", "track", [{"id": "quiet"}]),
    ]
    accepted, reasons, status = evaluate_quality(
        rows=rows,
        taste=_taste(),
        home_tab_diagnostics={
            "accepted": False,
            "lane_item_counts": {"all": 12, "chill": 8, "workout": 7, "focus": 2},
            "rejection_reasons": ["focus:below_min_items"],
        },
    )
    assert accepted is True
    assert status == "canonical"
    assert "home_tabs_not_accepted" not in reasons


def test_quality_rejects_identical_lane_payloads() -> None:
    rows = [
        DiscoveryRow("todays_pick", "Today's pick", "todays_pick", "track", [{"id": "today"}]),
        DiscoveryRow("featured_new_albums", "Featured albums", "featured_new_albums", "album", [{"id": "album"}]),
        DiscoveryRow("last_played", "Last played", "last_played", "track", [{"id": "last"}]),
        DiscoveryRow("frequently_listened", "Frequently listened", "frequently_listened", "track", [{"id": "freq"}]),
        DiscoveryRow("made_for_you", "Made for you", "made_for_you", "track", [{"id": "mix"}]),
        DiscoveryRow("because_you_played", "Because you played", "because_you_played", "track", [{"id": "because"}]),
        DiscoveryRow("recommended_albums", "Recommended albums", "recommended_albums", "album", [{"id": "new", "title": "New Album"}]),
        DiscoveryRow("quiet_picks", "Quiet Picks", "quiet_picks", "track", [{"id": "quiet"}]),
    ]
    accepted, reasons, _status = evaluate_quality(
        rows=rows,
        taste=_taste(),
        home_tab_diagnostics={
            "accepted": False,
            "lane_item_counts": {"all": 12, "chill": 12, "workout": 12, "focus": 12},
            "rejection_reasons": ["chill_workout:too_similar"],
        },
    )
    assert accepted is False
    assert "home_tabs_too_similar" in reasons


def test_quality_rejects_listened_only_album_discovery() -> None:
    taste = _taste()
    taste.recent_tracks.append({"id": "heard", "album": "Already Heard"})
    rows = [
        DiscoveryRow("todays_pick", "Today's pick", "todays_pick", "track", [{"id": "today"}]),
        DiscoveryRow("featured_new_albums", "Featured albums", "featured_new_albums", "album", [{"id": "album"}]),
        DiscoveryRow("last_played", "Last played", "last_played", "track", [{"id": "last"}]),
        DiscoveryRow("frequently_listened", "Frequently listened", "frequently_listened", "track", [{"id": "freq"}]),
        DiscoveryRow("made_for_you", "Made for you", "made_for_you", "track", [{"id": "mix"}]),
        DiscoveryRow("because_you_played", "Because you played", "because_you_played", "track", [{"id": "because"}]),
        DiscoveryRow("recommended_albums", "Recommended albums", "recommended_albums", "album", [{"id": "heard-album", "title": "Already Heard"}]),
        DiscoveryRow("quiet_picks", "Quiet Picks", "quiet_picks", "track", [{"id": "quiet"}]),
    ]
    accepted, reasons, _status = evaluate_quality(
        rows=rows,
        taste=taste,
        home_tab_diagnostics={
            "accepted": True,
            "lane_item_counts": {"all": 12, "chill": 12, "workout": 12, "focus": 12, "mood": 12},
        },
    )
    assert accepted is False
    assert "recommended_albums_listened_only" in reasons


def test_workout_lane_excludes_acoustic_slow_tracks() -> None:
    pools = {
        "similarity": [
            _track("a1", "Acoustic Sleep Ballad", "Soft A", "acoustic slow sleep piano ballad", "similarity"),
            _track("r1", "High Voltage Run", "Rock A", "rock upbeat live energy", "similarity"),
            _track("r2", "Dance Floor Sprint", "Pop A", "dance edm upbeat energy", "similarity"),
            _track("r3", "Metal Push", "Metal A", "metal hard rock energy", "similarity"),
        ],
        "artist_graph": [],
        "genre_mood": [],
        "collaborative": [],
        "popularity": [],
    }
    tracks = rank_tracks(
        pools,
        _taste(),
        ROW_RECIPES["made_for_you"],
        limit=4,
        lane_id="workout",
    )
    ids = {track["id"] for track in tracks}
    assert "a1" not in ids
    assert {"r1", "r2", "r3"} & ids


def test_workout_lane_rejects_low_energy_trait_despite_rock_keyword() -> None:
    low_energy = _track(
        "low-energy-rock",
        "Rock Bedtime",
        "Soft Rock",
        "rock official",
        "similarity",
    )
    low_energy.item["mood_axes"] = {"energy": 0.2, "drive": 0.2}
    high_energy = _track(
        "high-energy-rock",
        "Rock Sprint",
        "Fast Rock",
        "rock official",
        "similarity",
    )
    high_energy.item["mood_axes"] = {"energy": 0.85, "drive": 0.9}
    tracks = rank_tracks(
        {
            "similarity": [low_energy, high_energy],
            "artist_graph": [],
            "genre_mood": [],
            "collaborative": [],
            "popularity": [],
        },
        _taste(),
        ROW_RECIPES["made_for_you"],
        limit=4,
        lane_id="workout",
    )
    ids = {track["id"] for track in tracks}
    assert "low-energy-rock" not in ids
    assert "high-energy-rock" in ids


def test_todays_pick_is_part_of_discovery_row_contract() -> None:
    recipe = ROW_RECIPES["todays_pick"]
    assert recipe.title == "Today's pick"
    assert recipe.launch_required is True
    assert recipe.min_items == 1


def test_quiet_picks_are_taste_based_not_literal_quiet() -> None:
    pools = {
        "similarity": [
            _track("taste-1", "Thunder Rock Followup", "Band A", "hard rock metal energy", "similarity"),
            _track("quiet-1", "Soft Piano Sleep", "Soft A", "acoustic quiet piano sleep", "similarity"),
        ],
        "artist_graph": [],
        "genre_mood": [],
        "collaborative": [],
        "popularity": [],
    }
    tracks = rank_tracks(
        pools,
        _taste(),
        ROW_RECIPES["quiet_picks"],
        limit=2,
    )
    ids = [track["id"] for track in tracks]
    assert "taste-1" in ids
    assert "quiet-1" not in ids


def test_quiet_picks_fill_with_relevant_tracks_from_related_artist() -> None:
    pools = {
        "similarity": [],
        "artist_graph": [
            DiscoveryCandidate(
                item={
                    "id": f"related-{index}",
                    "title": f"Related Rock Song {index}",
                    "artist": "Adjacent Band",
                    "genre": "rock",
                    "artist_neighborhood": True,
                },
                source="artist_graph",
                score=3.0,
            )
            for index in range(6)
        ],
        "genre_mood": [],
        "collaborative": [],
        "popularity": [],
    }
    tracks = rank_tracks(
        pools,
        _taste(),
        ROW_RECIPES["quiet_picks"],
        limit=6,
    )
    assert len(tracks) == 6
    assert {track["artist"] for track in tracks} == {"Adjacent Band"}


def test_discovery_rows_reject_unrelated_popularity_only_candidates() -> None:
    pools = {
        "similarity": [
            _track("related", "Related Rock", "Band D", "rock", "similarity"),
        ],
        "artist_graph": [],
        "genre_mood": [],
        "popularity": [
            _track("random", "Unrelated Global Hit", "Unknown Artist", "foreign dance", "popularity"),
        ],
        "discovery_universe": [],
    }
    tracks = rank_tracks(
        pools,
        _taste(),
        ROW_RECIPES["because_you_played"],
        limit=8,
    )
    ids = {track["id"] for track in tracks}
    assert "related" in ids
    assert "random" not in ids


def test_featured_albums_caps_each_artist_to_one_album() -> None:
    pools = {
        "fresh_or_recent_albums": [
            _album(f"same-{index}", f"Same Artist Album {index}", "Band A", "rock", f"source-{index}")
            for index in range(4)
        ] + [
            _album("other-1", "Other Album", "Band D", "rock", "source-other"),
        ],
        "known_artist_albums": [],
        "adjacent_artist_albums": [],
        "genre_album_discovery": [],
        "classic_neighbor_albums": [],
        "album": [],
    }
    albums, _has_release_metadata = rank_albums(
        pools,
        _taste(),
        ROW_RECIPES["featured_new_albums"],
    )
    assert sum(1 for album in albums if album["artist"] == "Band A") == 1
    assert any(album["artist"] == "Band D" for album in albums)


def test_recommended_albums_exclude_featured_album_identities() -> None:
    pools = {
        "fresh_or_recent_albums": [
            _album("featured", "Featured Album", "Band A", "rock", "source-a"),
            _album("discovery", "Discovery Album", "Band B", "rock", "source-b"),
        ],
        "known_artist_albums": [],
        "adjacent_artist_albums": [],
        "genre_album_discovery": [],
        "classic_neighbor_albums": [],
        "album": [],
    }
    albums, _ = rank_albums(
        pools,
        _taste(),
        ROW_RECIPES["recommended_albums"],
        exclude_album_keys={"featured", "featured album|band a"},
    )
    assert [album["id"] for album in albums] == ["discovery"]


def test_quiet_picks_fill_beyond_short_row_artist_caps() -> None:
    taste = _taste()
    pools = {
        "similarity": [
            _track(
                f"quiet-{index}",
                f"Taste Match {index}",
                "Band A",
                "rock",
                "similarity",
            )
            for index in range(24)
        ],
    }
    tracks = rank_tracks(
        pools,
        taste,
        ROW_RECIPES["quiet_picks"],
        limit=24,
    )
    assert len(tracks) == 24


def test_quality_marks_strong_feed_launchable_when_genre_row_is_temporarily_unavailable() -> None:
    rows = [
        DiscoveryRow("todays_pick", "Today's pick", "todays_pick", "track", [{"id": "today"}]),
        DiscoveryRow("featured_new_albums", "Featured albums", "featured_new_albums", "album", [{"id": "album"}]),
        DiscoveryRow("last_played", "Last played", "last_played", "track", [{"id": "last"}]),
        DiscoveryRow("frequently_listened", "Frequently listened", "frequently_listened", "track", [{"id": "freq"}]),
        DiscoveryRow("made_for_you", "Made for you", "made_for_you", "track", [{"id": "mix"}]),
        DiscoveryRow("because_you_played", "Because you played", "because_you_played", "track", [{"id": "because"}]),
        DiscoveryRow("recommended_albums", "Albums", "recommended_albums", "album", [{"id": "album-2"}]),
        DiscoveryRow("recommended_artists", "Artists", "recommended_artists", "artist", [{"id": "artist"}]),
        DiscoveryRow("quiet_picks", "Quiet Picks", "quiet_picks", "track", [{"id": "quiet"}]),
    ]
    accepted, reasons, status = evaluate_quality(
        rows=rows,
        taste=_taste(),
        home_tab_diagnostics={
            "accepted": True,
            "lane_item_counts": {"all": 12, "chill": 12, "workout": 12, "focus": 12, "mood": 12},
        },
    )
    assert accepted is True
    assert status == "launchable"
    assert "missing_trending_by_genre" in reasons


def test_home_lanes_require_real_lane_payloads() -> None:
    chill_tracks = [
        _track(f"chill-{i}", f"Mellow {i}", f"Chill {i}", "acoustic mellow soul chill", "similarity")
        for i in range(16)
    ]
    workout_tracks = [
        _track(f"workout-{i}", f"Run {i}", f"Workout {i}", "rock dance upbeat energy", "similarity")
        for i in range(16)
    ]
    pools = {
        "similarity": [
            *chill_tracks,
            *workout_tracks,
            *[_track(f"focus-{i}", f"Study {i}", f"Focus {i}", "instrumental ambient piano study", "similarity") for i in range(16)],
            *[_track(f"mood-{i}", f"Mood {i}", f"Mood {i}", "emotional atmospheric cinematic soul", "similarity") for i in range(16)],
        ],
        "artist_graph": [],
        "genre_mood": [],
        "collaborative": [],
        "popularity": [],
        "album": [
            *[
                _album(f"chill-album-{i}", f"Quiet Album {i}", f"Chill {i}", "acoustic mellow soul chill", f"chill-{i}")
                for i in range(4)
            ],
            *[
                _album(f"workout-album-{i}", f"Energy Album {i}", f"Workout {i}", "rock dance upbeat energy", f"workout-{i}")
                for i in range(4)
            ],
        ],
        "freshness": [],
    }
    lanes, diagnostics = build_home_lanes(pools, _taste())
    assert diagnostics["accepted"] is True
    assert set(lanes) == {"all", "chill", "workout", "focus", "mood"}
    assert len(lanes["workout"]["tracks"]) >= 12
    chill_ids = {item["id"] for item in lanes["chill"]["tracks"][:12]}
    workout_ids = {item["id"] for item in lanes["workout"]["tracks"][:12]}
    assert len(chill_ids - workout_ids) >= 8
    assert lanes["chill"]["albums"][0]["id"].startswith("chill-album-")
    assert lanes["workout"]["albums"][0]["id"].startswith("workout-album-")


def test_home_lanes_prefer_separate_retrieved_lane_pools() -> None:
    pools = {
        "similarity": [
            _track(f"all-{index}", f"General Pick {index}", f"General {index}", "popular official", "similarity")
            for index in range(24)
        ],
        "artist_graph": [],
        "genre_mood": [],
        "collaborative": [],
        "popularity": [],
        "album": [],
        "freshness": [],
    }
    lane_text = {
        "chill": "acoustic mellow chill soul",
        "workout": "rock dance upbeat energy",
        "focus": "instrumental ambient piano study",
        "mood": "emotional atmospheric cinematic mood",
    }
    for lane_id, text in lane_text.items():
        pools[f"lane_{lane_id}"] = [
            _track(
                f"{lane_id}-{index}",
                f"{lane_id.title()} Pick {index}",
                f"{lane_id.title()} Artist {index}",
                text,
                f"lane_{lane_id}",
            )
            for index in range(16)
        ]

    lanes, diagnostics = build_home_lanes(pools, _taste())

    assert diagnostics["accepted"] is True
    assert diagnostics["lane_pool_counts"] == {"all": 0, "chill": 16, "workout": 16, "focus": 16, "mood": 16}
    for lane_id in lane_text:
        assert diagnostics["lane_pool_selected_counts"][lane_id] >= 12
        assert all(track["id"].startswith(f"{lane_id}-") for track in lanes[lane_id]["tracks"])


def test_genre_mood_retrieval_emits_distinct_reusable_lane_pools() -> None:
    candidates, timings = _genre_mood_pool(_LaneSearchServer(), _taste())
    by_source = {
        source: [candidate for candidate in candidates if candidate.source == source]
        for source in ("genre_mood", "lane_chill", "lane_workout", "lane_focus", "lane_mood")
    }

    assert all(by_source.values())
    lane_ids = [
        {candidate.item["id"] for candidate in by_source[source]}
        for source in ("lane_chill", "lane_workout", "lane_focus", "lane_mood")
    ]
    assert all(left.isdisjoint(right) for index, left in enumerate(lane_ids) for right in lane_ids[index + 1 :])
    assert any(key.startswith("lane_chill:") for key in timings)
    assert any(key.startswith("genre_mood:") for key in timings)


def test_album_discovery_segments_cover_distinct_provenance() -> None:
    taste = _taste()
    current_year = datetime.now(timezone.utc).year
    candidates = [
        DiscoveryCandidate(
            item={"id": "known", "title": "Known", "artist": "Band A"},
            source="album",
            item_type="album",
        ),
        DiscoveryCandidate(
            item={"id": "adjacent", "title": "Adjacent", "artist": "Neighbor", "artist_neighborhood": True},
            source="artist_graph",
            item_type="album",
        ),
        DiscoveryCandidate(
            item={"id": "genre", "title": "Genre", "artist": "New Artist"},
            source="genre_mood",
            item_type="album",
        ),
        DiscoveryCandidate(
            item={"id": "fresh", "title": "Fresh", "artist": "New Artist", "release_date": str(current_year)},
            source="similarity",
            item_type="album",
        ),
        DiscoveryCandidate(
            item={"id": "classic", "title": "Classic", "artist": "Neighbor", "release_date": "1997"},
            source="similarity",
            item_type="album",
        ),
    ]
    for candidate in candidates:
        candidate.item["album_segment"] = _album_segment_for_candidate(candidate, taste)

    segments = _album_segment_pools(candidates)

    assert {items[0].item["id"] for items in segments.values()} == {
        "known",
        "adjacent",
        "genre",
        "fresh",
        "classic",
    }


def test_album_ranking_penalizes_exact_listened_album() -> None:
    taste = _taste()
    taste.recent_tracks.append(
        {"id": "heard-track", "title": "Heard Song", "artist": "Band A", "album": "Already Heard"}
    )
    pools = {
        "adjacent_artist_albums": [
            DiscoveryCandidate(
                item={"id": "new-album", "title": "Neighbor Album", "artist": "Neighbor"},
                source="album",
                score=5.0,
                item_type="album",
            )
        ],
        "known_artist_albums": [
            DiscoveryCandidate(
                item={"id": "heard-album", "title": "Already Heard", "artist": "Band A"},
                source="album",
                score=8.0,
                item_type="album",
            )
        ],
    }

    albums, _ = rank_albums(pools, taste, ROW_RECIPES["recommended_albums"])

    assert [album["id"] for album in albums[:2]] == ["new-album", "heard-album"]


def test_trending_genre_tabs_do_not_fill_named_tabs_with_unrelated_tracks() -> None:
    pools = {
        "similarity": [
            *[_track(f"sparse-{i}", f"Discovery Pick {i}", f"Artist {i}", "catalog favorite official", "similarity") for i in range(30)],
        ],
        "genre_mood": [],
        "popularity": [],
    }
    tabs, diagnostics = build_trending_genre_tabs(pools, _taste())
    assert diagnostics["accepted"] is False
    assert tabs == []


def test_trending_genre_tabs_require_matching_provenance() -> None:
    jazz = [
        DiscoveryCandidate(
            item={
                "id": f"jazz-{index}",
                "title": f"Jazz Session {index}",
                "artist": f"Jazz Artist {index}",
                "discovery_genres": ["jazz"],
            },
            source="genre_mood",
            score=2.0,
        )
        for index in range(8)
    ]
    rock = [
        DiscoveryCandidate(
            item={
                "id": f"rock-{index}",
                "title": f"Rock Session {index}",
                "artist": f"Rock Artist {index}",
                "discovery_genres": ["rock"],
            },
            source="genre_mood",
            score=2.0,
        )
        for index in range(8)
    ]
    tabs, diagnostics = build_trending_genre_tabs(
        {"similarity": [], "genre_mood": [*jazz, *rock], "popularity": []},
        _taste(),
    )
    assert diagnostics["accepted"] is True
    by_id = {tab["id"]: tab for tab in tabs}
    assert {track["id"] for track in by_id["jazz"]["tracks"]} == {
        f"jazz-{index}" for index in range(8)
    }
    assert {track["id"] for track in by_id["rock"]["tracks"]} == {
        f"rock-{index}" for index in range(8)
    }


def test_artist_name_accepts_canonical_and_legacy_channel_fields() -> None:
    assert artist_name({"artist": "Canonical Artist"}) == "Canonical Artist"
    assert artist_name({"channel": "Legacy Channel"}) == "Legacy Channel"


def test_track_normalization_preserves_catalog_genre_and_quality_evidence() -> None:
    queen = normalize_track(
        _FeatureServer(),
        {
            "id": "queen-1",
            "title": "Bohemian Rhapsody",
            "artist": "Queen",
            "album": "A Night at the Opera",
        },
    )
    tribute = normalize_track(
        _FeatureServer(),
        {
            "id": "tribute-1",
            "title": "Highway to Hell Lounge Tribute",
            "artist": "Various Artists",
        },
    )
    assert queen is not None and queen["genre"] == "rock"
    assert queen["canonical_title_artist_identity"] == "bohemian rhapsody|queen"
    assert queen["catalog_feature_version"] == "catalog-feature-v1"
    assert queen["mood_axes"]["energy"] > 0.5
    assert tribute is not None and tribute["discovery_quality_penalty"] >= 3.0


def test_generic_search_track_is_not_treated_as_verified_catalog() -> None:
    track = normalize_track(
        _FeatureServer(),
        {
            "id": "generic-search-result",
            "title": "Relax With Me",
            "artist": "Unknown Upload Channel",
        },
    )
    assert track is not None
    assert track["source_authority"] == "unknown"


def test_strict_discovery_sources_require_authoritative_tracks() -> None:
    pool = []
    seen: set[str] = set()
    _add_candidate(
        _FeatureServer(),
        pool,
        seen,
        {
            "id": "generic-search-result",
            "title": "Relax With Me",
            "artist": "Unknown Upload Channel",
        },
        source="lane_chill",
        score=2.0,
        reason="chill songs",
    )
    _add_candidate(
        _FeatureServer(),
        pool,
        seen,
        {
            "id": "official-result",
            "title": "Official Song",
            "artist": "Known Artist - Topic",
        },
        source="lane_chill",
        score=2.0,
        reason="chill songs",
    )
    assert [candidate.item["id"] for candidate in pool] == ["official-result"]


def test_unofficial_search_media_is_rejected_from_discovery_rows() -> None:
    tracks = rank_tracks(
        {
            "similarity": [
                DiscoveryCandidate(
                    item={
                        "id": "unofficial",
                        "title": "Acoustic Cover",
                        "artist": "Cover Channel",
                        "source_authority": "search_only",
                    },
                    source="similarity",
                    score=9.0,
                ),
                _track(
                    "official",
                    "Thunder Rock Followup",
                    "Band A",
                    "hard rock official",
                    "similarity",
                ),
            ],
        },
        _taste(),
        ROW_RECIPES["made_for_you"],
        limit=4,
    )
    assert [track["id"] for track in tracks] == ["official"]


def test_album_enrichment_uses_release_date_and_canonical_identity() -> None:
    album = _album_item_from_track(
        _FeatureServer(),
        {
            "id": "track-1",
            "title": "Song",
            "artist": "Queen",
            "album": "A Night at the Opera",
            "release_date": "1975-11-21",
        },
    )
    assert album is not None
    assert album["release_year"] == 1975
    assert album["era_bucket"] == "1970s"
    assert album["canonical_album_identity"] == "a night at the opera::queen"
    assert album["normalized_title"] == "a night at the opera"
    assert album["normalized_artist_name"] == "queen"


def test_track_enrichment_preserves_artist_graph_and_audio_trait_provenance() -> None:
    track = normalize_track(
        _FeatureServer(),
        {
            "id": "neighbor-track",
            "title": "Rock Neighbor",
            "artist": "Adjacent Band - Topic",
            "album": "Neighbor Album",
            "related_to_artist": "Queen",
            "mood_axes": {"energy": 0.88, "drive": 0.8},
            "language": "english",
            "region": "uk",
            "popularity": 0.72,
        },
    )
    assert track is not None
    assert track["audio_traits"]["energy"] == 0.88
    assert track["mood_axes"]["drive"] == 0.8
    assert track["language"] == "english"
    assert track["region"] == "uk"
    assert track["popularity"] == 0.72
    assert "Queen" in track["peer_artist_ids"]
    assert track["source_authority"] == "official"


def test_discovery_ranking_prefers_supported_language_and_authoritative_source() -> None:
    taste = _taste()
    taste.source_profile = {
        "supported_languages": ["english"],
        "supported_regions": ["global"],
    }
    tracks = rank_tracks(
        {
            "similarity": [
                DiscoveryCandidate(
                    item={
                        "id": "weak-foreign",
                        "title": "Unrelated Track",
                        "artist": "Unknown Artist",
                        "language": "spanish",
                        "region": "latin_america",
                        "source_authority": "unknown",
                    },
                    source="similarity",
                    score=5.0,
                ),
                DiscoveryCandidate(
                    item={
                        "id": "official-neighbor",
                        "title": "Rock Neighbor",
                        "artist": "Known Band - Topic",
                        "language": "english",
                        "region": "global",
                        "source_authority": "official",
                    },
                    source="similarity",
                    score=5.0,
                ),
            ]
        },
        taste,
        ROW_RECIPES["made_for_you"],
        limit=2,
    )
    assert [track["id"] for track in tracks] == [
        "official-neighbor",
        "weak-foreign",
    ]


def test_home_response_keeps_existing_contract_shape() -> None:
    now = time.time()
    artifact = DiscoveryArtifact(
        session_id="session-a",
        user_scope_id="user-a",
        profile_key="profile-a",
        generated_at=now,
        expires_at=now + 3600,
        rows=[
            DiscoveryRow(
                "made_for_you",
                "Made for you",
                "made_for_you",
                "track",
                [{"id": "a", "title": "A", "artist": "Artist"}],
            )
        ],
        diagnostics={"engine": "discovery_engine"},
        candidate_pool_counts={"similarity": 1},
        provider_timings_ms={"similarity": 1},
        home_tab_lanes={},
        accepted=True,
    )
    response = home_response_from_artifact(artifact, request_id="request-a", page_size=8)
    assert response["status"] == "success"
    assert response["request_id"] == "request-a"
    assert response["session_id"] == "session-a"
    assert response["rows"][0]["kind"] == "made_for_you"
    assert response["shelves"][0]["kind"] == "made_for_you"
    assert response["recommendations"][0]["id"] == "a"
    assert response["diagnostics"]["engine"] == "discovery_engine"


def test_home_response_preserves_kept_previous_quality_status() -> None:
    now = time.time()
    artifact = DiscoveryArtifact(
        session_id="session-kept",
        user_scope_id="user-a",
        profile_key="profile-a",
        generated_at=now,
        expires_at=now + 3600,
        rows=[
            DiscoveryRow(
                "made_for_you",
                "Made for you",
                "made_for_you",
                "track",
                [{"id": "a", "title": "A", "artist": "Artist"}],
            )
        ],
        diagnostics={"engine": "discovery_engine", "artifact_quality": "kept_previous"},
        candidate_pool_counts={},
        provider_timings_ms={},
        home_tab_lanes={},
        accepted=True,
        artifact_source="cache",
    )
    response = home_response_from_artifact(artifact, request_id="request-kept", page_size=8)
    assert response["diagnostics"]["artifact_quality"] == "kept_previous"


def test_background_build_guard_suppresses_inflight_and_recent_duplicate() -> None:
    service = DiscoveryService(object())
    fingerprint = "user-a:profile-a"
    assert service._claim_background_build(fingerprint) is True
    assert service._claim_background_build(fingerprint) is False
    service._release_background_build(fingerprint)
    assert service._claim_background_build(fingerprint) is False


def test_balanced_discovery_universe_uses_bounded_source_quotas() -> None:
    pools = {
        source: [
            _track(
                f"{source}-{index}",
                f"{source} pick {index}",
                f"{source} artist {index}",
                "rock discovery",
                source,
            )
            for index in range(quota + 20)
        ]
        for source, quota in TRACK_POOL_QUOTAS.items()
    }

    universe, selected_by_source = _balanced_track_universe(pools)

    assert len(universe) == DISCOVERY_UNIVERSE_TARGET
    assert len({candidate.item["id"] for candidate in universe}) == len(universe)
    assert all(selected_by_source[source] <= quota for source, quota in TRACK_POOL_QUOTAS.items())
    assert all(candidate.source == "discovery_universe" for candidate in universe)


def test_personal_mixes_are_materialized_and_meaningfully_distinct() -> None:
    pools = {
        source: [
            _track(
                f"{source}-{index}",
                f"{source.title()} Pick {index}",
                f"{source.title()} Artist {index}",
                "rock pop soul discovery official",
                source,
            )
            for index in range(48)
        ]
        for source in (
            "history",
            "similarity",
            "artist_graph",
            "genre_mood",
            "collaborative",
            "popularity",
            "discovery_universe",
        )
    }

    mixes, diagnostics = build_personal_mixes(pools, _taste())

    assert 3 <= len(mixes) <= 5
    assert diagnostics["mix_count"] == len(mixes)
    assert all(len(mix["tracks"]) >= 8 for mix in mixes)
    for index, left in enumerate(mixes):
        left_ids = {track["id"] for track in left["tracks"]}
        for right in mixes[index + 1 :]:
            right_ids = {track["id"] for track in right["tracks"]}
            overlap = len(left_ids & right_ids)
            assert overlap / max(min(len(left_ids), len(right_ids)), 1) <= 0.40


def test_artifact_retains_row_reserve_while_home_and_row_page_are_sliced() -> None:
    now = time.time()
    items = [{"id": f"track-{index}", "title": f"Track {index}"} for index in range(30)]
    artifact = DiscoveryArtifact(
        session_id="session-paging",
        user_scope_id="user-a",
        profile_key="profile-a",
        generated_at=now,
        expires_at=now + 3600,
        rows=[
            DiscoveryRow(
                "because_you_played",
                "Because you played",
                "because_you_played",
                "track",
                items,
                meta={"page_size": 8, "prepared_count": 30, "reserve_count": 22},
                next_offset=8,
                has_more=True,
            )
        ],
        diagnostics={"engine": "discovery_engine"},
        candidate_pool_counts={},
        provider_timings_ms={},
        home_tab_lanes={},
        accepted=True,
    )

    restored = _artifact_from_dict(artifact_to_dict(artifact))
    assert restored is not None
    assert len(restored.rows[0].items) == 30
    assert restored.rows[0].has_more is True

    home = home_response_from_artifact(restored, request_id="home", page_size=8)
    assert len(home["rows"][0]["items"]) == 8
    assert home["rows"][0]["has_more"] is True
    page = row_page_response_from_artifact(
        restored,
        row_id="because_you_played",
        offset=8,
        limit=12,
        request_id="page",
    )
    assert page is not None
    assert len(page["recommendations"]) == 12
    assert page["next_offset"] == 20
    assert page["has_more"] is True
