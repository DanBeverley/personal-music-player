from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

from auralis_backend.discovery.adapters import home_response_from_artifact, row_page_response_from_artifact
from auralis_backend.discovery.artifact import (
    _artifact_from_dict,
    artifact_to_dict,
    evaluate_quality,
    row_shortage_domains,
)
from auralis_backend.discovery.config import DISCOVERY_UNIVERSE_TARGET, ROW_RECIPES, TRACK_POOL_QUOTAS
from auralis_backend.discovery.candidates import (
    _add_candidate,
    _balanced_track_universe,
    _album_segment_for_candidate,
    _album_segment_pools,
    artist_name,
    normalize_track,
)
from auralis_backend.discovery.ranking import (
    _finalize_allocated_rows,
    _radio_relation_for_candidate,
    build_home_lanes,
    build_personal_mixes,
    build_popular_radio_cards,
    rank_albums,
    rank_tracks,
)
from auralis_backend.discovery.feed_state import (
    FeedState,
    invalidate_feed_state,
    load_feed_state,
    promote_prepared_feed,
    save_feed_state,
    store_active_feed,
    store_prepared_feed,
)
from auralis_backend.discovery.allocation import allocate_home_rows
from auralis_backend.discovery.inventory import (
    CandidateInventory,
    apply_inventory_intent_delta,
    merge_candidate_inventories,
)
from auralis_backend.discovery.schema import DiscoveryArtifact, DiscoveryCandidate, DiscoveryRow, TasteProfile
from auralis_backend.discovery.service import DiscoveryService
from auralis_backend.storage.session_store import get_session_store


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
            "source_authority": "verified_catalog",
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
            "source_authority": "verified_catalog",
            "relation_type": "same_genre_family",
            "relation_strength": 0.7,
        },
        source="album",
        score=2.0,
        item_type="album",
    )


def _row_items(prefix: str, count: int) -> list[dict[str, str]]:
    return [{"id": f"{prefix}-{index}"} for index in range(count)]


def _radio_inventory_pools(pools: dict[str, list[DiscoveryCandidate]]) -> dict[str, list[DiscoveryCandidate]]:
    output = dict(pools)

    def partition(values, name: str):
        return [
            DiscoveryCandidate(
                item={**candidate.item, "radio_partition": name},
                source=candidate.source,
                score=candidate.score,
                reasons=list(candidate.reasons),
                item_type=candidate.item_type,
            )
            for candidate in values or []
        ]

    output["radio_artist_catalog"] = partition(pools.get("profile_spine", []), "artist_catalog")
    output["radio_taste"] = partition(pools.get("artist_graph", []) + pools.get("similarity", []), "taste_mix")
    output["radio_discovery"] = partition(pools.get("artist_graph", []) + pools.get("similarity", []), "discovery_mix")
    return output


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
    assert status == "build_failed"
    assert "weak_two_row_feed" in reasons


def test_quality_rejects_feed_without_popular_radio() -> None:
    rows = [
        DiscoveryRow("todays_pick", "Today's pick", "todays_pick", "track", [{"id": "today"}]),
        DiscoveryRow("featured_new_albums", "Featured albums", "featured_new_albums", "album", [{"id": "album"}]),
        DiscoveryRow("last_played", "Last played", "last_played", "track", [{"id": "last"}]),
        DiscoveryRow("frequently_listened", "Frequently listened", "frequently_listened", "track", [{"id": "freq"}]),
        DiscoveryRow("made_for_you", "Made for you", "made_for_you", "mix", _row_items("mix", 3)),
        DiscoveryRow("because_you_played", "Because you played", "because_you_played", "track", [{"id": "because"}]),
        DiscoveryRow("recommended_albums", "Recommended albums", "recommended_albums", "album", [{"id": "album-2"}]),
        DiscoveryRow("recommended_artists", "Recommended artists", "recommended_artists", "artist", [{"id": "artist"}]),
        DiscoveryRow("quiet_picks", "Quiet Picks", "quiet_picks", "track", _row_items("quiet", 32)),
    ]
    accepted, reasons, status = evaluate_quality(
        rows=rows,
        taste=_taste(),
        home_tab_diagnostics={
            "accepted": True,
            "lane_item_counts": {"all": 12, "chill": 12, "workout": 12, "focus": 12, "mood": 12},
        },
    )
    assert accepted is False
    assert status == "build_failed"
    assert "missing_popular_radio" in reasons


def test_quality_accepts_strong_feed_with_partial_usable_lanes() -> None:
    rows = [
        DiscoveryRow("todays_pick", "Today's pick", "todays_pick", "track", [{"id": "today"}]),
        DiscoveryRow("featured_new_albums", "Featured albums", "featured_new_albums", "album", [{"id": "album"}]),
        DiscoveryRow("last_played", "Last played", "last_played", "track", [{"id": "last"}]),
        DiscoveryRow("frequently_listened", "Frequently listened", "frequently_listened", "track", [{"id": "freq"}]),
        DiscoveryRow("made_for_you", "Made for you", "made_for_you", "mix", _row_items("mix", 3)),
        DiscoveryRow("because_you_played", "Because you played", "because_you_played", "track", [{"id": "because"}]),
        DiscoveryRow(
            "popular_radio",
            "Popular Radio",
            "popular_radio",
            "radio",
            _row_items("radio", 5),
        ),
        DiscoveryRow("recommended_albums", "Recommended albums", "recommended_albums", "album", [{"id": "album-2"}]),
        DiscoveryRow("quiet_picks", "Quiet Picks", "quiet_picks", "track", _row_items("quiet", 32)),
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
    assert status == "servable"
    assert "home_tabs_not_accepted" not in reasons


def test_quality_no_longer_rejects_identical_lane_payloads() -> None:
    rows = [
        DiscoveryRow("todays_pick", "Today's pick", "todays_pick", "track", [{"id": "today"}]),
        DiscoveryRow("featured_new_albums", "Featured albums", "featured_new_albums", "album", [{"id": "album"}]),
        DiscoveryRow("last_played", "Last played", "last_played", "track", [{"id": "last"}]),
        DiscoveryRow("frequently_listened", "Frequently listened", "frequently_listened", "track", [{"id": "freq"}]),
        DiscoveryRow("made_for_you", "Made for you", "made_for_you", "mix", _row_items("mix", 3)),
        DiscoveryRow("because_you_played", "Because you played", "because_you_played", "track", [{"id": "because"}]),
        DiscoveryRow("popular_radio", "Popular Radio", "popular_radio", "radio", _row_items("radio", 5)),
        DiscoveryRow("recommended_albums", "Recommended albums", "recommended_albums", "album", [{"id": "new", "title": "New Album"}]),
        DiscoveryRow("quiet_picks", "Quiet Picks", "quiet_picks", "track", _row_items("quiet", 32)),
    ]
    accepted, reasons, status = evaluate_quality(
        rows=rows,
        taste=_taste(),
        home_tab_diagnostics={
            "accepted": False,
            "lane_item_counts": {"all": 12, "chill": 12, "workout": 12, "focus": 12},
            "rejection_reasons": ["chill_workout:too_similar"],
        },
    )
    assert accepted is True
    assert status == "servable"
    assert "home_tabs_too_similar" not in reasons


def test_quality_rejects_listened_only_thin_album_discovery() -> None:
    taste = _taste()
    taste.recent_tracks.append({"id": "heard", "album": "Already Heard"})
    rows = [
        DiscoveryRow("todays_pick", "Today's pick", "todays_pick", "track", [{"id": "today"}]),
        DiscoveryRow("featured_new_albums", "Featured albums", "featured_new_albums", "album", [{"id": "album"}]),
        DiscoveryRow("last_played", "Last played", "last_played", "track", [{"id": "last"}]),
        DiscoveryRow("frequently_listened", "Frequently listened", "frequently_listened", "track", [{"id": "freq"}]),
        DiscoveryRow("made_for_you", "Made for you", "made_for_you", "mix", _row_items("mix", 3)),
        DiscoveryRow("because_you_played", "Because you played", "because_you_played", "track", [{"id": "because"}]),
        DiscoveryRow("popular_radio", "Popular Radio", "popular_radio", "radio", _row_items("radio", 5)),
        DiscoveryRow("recommended_albums", "Recommended albums", "recommended_albums", "album", [{"id": "heard-album", "title": "Already Heard"}]),
        DiscoveryRow("quiet_picks", "Quiet Picks", "quiet_picks", "track", _row_items("quiet", 32)),
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
    assert _status == "build_failed"
    assert "missing_required_rows" in reasons
    assert "recommended_albums_listened_only" in reasons


def test_album_discovery_rejects_unrelated_editorial_albums() -> None:
    taste = _taste()
    related = DiscoveryCandidate(
        item={
            "id": "related-album",
            "title": "Related Rock Album",
            "artist": "Band A",
            "genre": "rock",
            "source_authority": "official",
            "album_source": "artist_catalog",
        },
        source="album",
        score=3.0,
        item_type="album",
    )
    unrelated = DiscoveryCandidate(
        item={
            "id": "unrelated-editorial",
            "title": "Unrelated Editorial Pick",
            "artist": "Faraway Pop Artist",
            "genre": "kpop",
            "source_authority": "official",
            "album_source": "editorial_album_pick",
        },
        source="album",
        score=9.0,
        item_type="album",
    )

    albums, _has_release_metadata = rank_albums(
        {"album": [unrelated, related]},
        taste,
        ROW_RECIPES["recommended_albums"],
    )

    ids = {album.get("id") for album in albums}
    assert "related-album" in ids
    assert "unrelated-editorial" not in ids
    related_album = next(album for album in albums if album.get("id") == "related-album")
    assert related_album.get("album_relation_reason") == "known_artist"


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


def test_force_refresh_penalizes_visible_track_ids_for_mutable_rows() -> None:
    taste = _taste()
    taste.force_refresh = True
    taste.refresh_token = "refresh-a"
    taste.avoid_ids = ["visible-1"]
    tracks = rank_tracks(
        {
            "similarity": [
                _track("visible-1", "Already Visible", "Band A", "rock official", "similarity"),
                _track("fresh-1", "Fresh Neighbor", "Band C", "rock official", "similarity"),
            ],
            "artist_graph": [],
            "genre_mood": [],
            "collaborative": [],
            "popularity": [],
        },
        taste,
        ROW_RECIPES["todays_pick"],
        limit=1,
    )
    assert tracks[0]["id"] == "fresh-1"


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
            _track("related", "Related Rock", "Band D", "rock", "artist_graph"),
        ],
        "artist_graph": [],
        "genre_mood": [],
        "popularity": [
            _track("random", "Unrelated Global Hit", "Unknown Artist", "foreign dance", "popularity"),
        ],
        "discovery_universe": [],
    }
    pools["similarity"][0].item["source_authority"] = "official"
    pools["similarity"][0].item["artist_graph_keys"] = ["Band A"]
    tracks = rank_tracks(
        pools,
        _taste(),
        ROW_RECIPES["because_you_played"],
        limit=8,
    )
    ids = {track["id"] for track in tracks}
    assert "related" in ids
    assert "random" not in ids


def test_exploratory_sources_reject_off_profile_language_even_with_pop_genre() -> None:
    taste = _taste()
    taste.source_profile = {
        "supported_languages": ["english"],
        "supported_regions": ["us", "gb", "global"],
    }
    tracks = rank_tracks(
        {
            "similarity": [
                _track("related", "Related Rock", "Band A", "rock", "similarity"),
            ],
            "genre_mood": [
                DiscoveryCandidate(
                    item={
                        "id": "bollywood-pop",
                        "title": "Kesariya",
                        "artist": "Arijit Singh",
                        "genre": "pop",
                        "language": "hindi",
                        "region": "india",
                        "source_authority": "verified_catalog",
                    },
                    source="genre_mood",
                    score=9.0,
                ),
            ],
        },
        taste,
        ROW_RECIPES["because_you_played"],
        limit=8,
    )
    ids = {track["id"] for track in tracks}
    assert "related" in ids
    assert "bollywood-pop" not in ids


def test_lane_sources_reject_off_profile_language_candidates() -> None:
    taste = _taste()
    taste.source_profile = {"supported_languages": ["english"]}
    tracks = rank_tracks(
        {
            "lane_chill": [
                DiscoveryCandidate(
                    item={
                        "id": "hindi-chill",
                        "title": "Bheegi Bheegi",
                        "artist": "Pritam",
                        "genre": "pop",
                        "language": "hindi",
                        "region": "india",
                        "source_authority": "verified_catalog",
                    },
                    source="lane_chill",
                    score=9.0,
                ),
                DiscoveryCandidate(
                    item={
                        "id": "english-chill",
                        "title": "Mellow Rock",
                        "artist": "Band A",
                        "genre": "rock",
                        "language": "english",
                        "source_authority": "official",
                    },
                    source="lane_chill",
                    score=3.0,
                ),
            ],
        },
        taste,
        ROW_RECIPES["made_for_you"],
        limit=4,
        lane_id="chill",
    )
    ids = {track["id"] for track in tracks}
    assert "english-chill" in ids
    assert "hindi-chill" not in ids


def test_exploratory_sources_reject_unknown_authority_without_personal_match() -> None:
    taste = _taste()
    taste.source_profile = {
        "supported_languages": ["english"],
        "supported_regions": ["us", "gb", "global"],
    }
    tracks = rank_tracks(
        {
            "lane_chill": [
                DiscoveryCandidate(
                    item={
                        "id": "anonymous-chill",
                        "title": "Soft Evening Mix",
                        "artist": "Unknown Channel",
                        "genre": "",
                        "language": "unknown",
                        "region": "unknown",
                        "source_authority": "unknown",
                    },
                    source="lane_chill",
                    score=9.0,
                ),
                DiscoveryCandidate(
                    item={
                        "id": "trusted-rock",
                        "title": "Trusted Rock",
                        "artist": "Band A",
                        "genre": "rock",
                        "language": "english",
                        "region": "global",
                        "source_authority": "official",
                    },
                    source="lane_chill",
                    score=3.0,
                ),
            ],
        },
        taste,
        ROW_RECIPES["made_for_you"],
        limit=4,
        lane_id="chill",
    )
    ids = {track["id"] for track in tracks}
    assert "trusted-rock" in ids
    assert "anonymous-chill" not in ids


def test_strict_candidate_sources_reject_search_only_background_music() -> None:
    pool = []
    seen = set()
    _add_candidate(
        _FeatureServer(),
        pool,
        seen,
        {
            "id": "cafe-track",
            "title": "Coffee Shop Music Relax Jazz",
            "artist": "Restaurant Background Music",
            "provider": "youtube_music",
        },
        source="ytmusic_home",
        score=2.0,
        reason="ytmusic_home_seed",
    )
    assert pool == []


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


def test_force_refresh_penalizes_visible_album_identities() -> None:
    taste = _taste()
    taste.force_refresh = True
    taste.refresh_token = "refresh-album"
    taste.avoid_ids = ["visible-album", "visible album|band a"]
    pools = {
        "fresh_or_recent_albums": [
            _album("visible-album", "Visible Album", "Band A", "rock", "source-a"),
            _album("fresh-album", "Fresh Album", "Band C", "rock", "source-b"),
        ],
        "known_artist_albums": [],
        "adjacent_artist_albums": [],
        "genre_album_discovery": [],
        "classic_neighbor_albums": [],
        "album": [],
    }
    albums, _ = rank_albums(
        pools,
        taste,
        ROW_RECIPES["featured_new_albums"],
    )
    assert albums[0]["id"] == "fresh-album"


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


def test_because_you_played_rejects_title_only_artist_name_matches() -> None:
    taste = TasteProfile(
        user_scope_id="user-ufo",
        profile_key="profile-ufo",
        signal_tier="known",
        recent_tracks=[
            {"id": "seed-ufo", "title": "Doctor Doctor", "artist": "UFO", "genre": "hard rock"},
        ],
        top_tracks=[],
        artist_hints=["UFO"],
    )
    title_only = _track("wrong-ufo", "UFO", "Random Channel", "dance pop", "similarity")
    related = _track("right-rock", "Rock Bottom", "Michael Schenker Group", "hard rock", "similarity")
    related.item["source_authority"] = "official"
    related.item["artist_graph_keys"] = ["UFO"]
    tracks = rank_tracks(
        {"similarity": [title_only, related], "profile_spine": [], "artist_graph": []},
        taste,
        ROW_RECIPES["because_you_played"],
        limit=8,
    )

    assert [track["id"] for track in tracks] == ["right-rock"]
    assert tracks[0]["recommendation_relation"] == "artist_neighbor"


def test_because_you_played_rejects_kids_content_without_audience_signal() -> None:
    taste = _taste()
    kids_track = _track("kids-police-car", "Police Car", "Pinkfong", "pop", "artist_graph")
    kids_track.item.update(
        {
            "source_authority": "official",
            "audience_profile": "kids_family",
            "audience_confidence": 0.94,
            "audience_source": "text_hint",
        }
    )
    related = _track("adult-pop", "Bright Neighbor", "Band B", "pop", "artist_graph")
    related.item.update(
        {
            "source_authority": "official",
            "artist_graph_keys": ["Band B"],
            "audience_profile": "general",
            "audience_confidence": 0.72,
        }
    )

    tracks = rank_tracks(
        {"artist_graph": [kids_track, related], "similarity": [], "profile_spine": []},
        taste,
        ROW_RECIPES["because_you_played"],
        limit=8,
    )

    ids = {track["id"] for track in tracks}
    assert "kids-police-car" not in ids
    assert "adult-pop" in ids


def test_because_you_played_allows_kids_content_with_audience_signal() -> None:
    taste = _taste()
    taste.source_profile = {"accepted_audience_profiles": ["general", "kids_family"]}
    kids_track = _track("kids-police-car", "Police Car", "Pinkfong", "pop", "artist_graph")
    kids_track.item.update(
        {
            "source_authority": "official",
            "audience_profile": "kids_family",
            "audience_confidence": 0.94,
            "audience_source": "text_hint",
        }
    )

    tracks = rank_tracks(
        {"artist_graph": [kids_track], "similarity": [], "profile_spine": []},
        taste,
        ROW_RECIPES["because_you_played"],
        limit=8,
    )

    assert [track["id"] for track in tracks] == ["kids-police-car"]


def test_rank_tracks_suppresses_exact_remove_from_feed_feedback() -> None:
    taste = _taste()
    taste.source_profile = {
        "negative_feedback": {
            "by_type": {
                "exact_track": {
                    "hidden-track": 1.0,
                }
            }
        }
    }
    hidden = _track("hidden-track", "Hidden Track", "Band A", "rock", "similarity")
    visible = _track("visible-track", "Visible Track", "Band A", "rock", "similarity")

    tracks = rank_tracks(
        {"similarity": [hidden, visible], "artist_graph": [], "genre_mood": []},
        taste,
        ROW_RECIPES["todays_pick"],
        limit=4,
    )

    assert [track["id"] for track in tracks] == ["visible-track"]


def test_quality_marks_missing_radio_row_as_fatal_when_other_rows_are_strong() -> None:
    rows = [
        DiscoveryRow("todays_pick", "Today's pick", "todays_pick", "track", [{"id": "today"}]),
        DiscoveryRow("featured_new_albums", "Featured albums", "featured_new_albums", "album", [{"id": "album"}]),
        DiscoveryRow("last_played", "Last played", "last_played", "track", [{"id": "last"}]),
        DiscoveryRow("frequently_listened", "Frequently listened", "frequently_listened", "track", [{"id": "freq"}]),
        DiscoveryRow("made_for_you", "Made for you", "made_for_you", "track", [{"id": "mix"}]),
        DiscoveryRow("because_you_played", "Because you played", "because_you_played", "track", [{"id": "because"}]),
        DiscoveryRow("recommended_albums", "Albums", "recommended_albums", "album", [{"id": "album-2"}]),
        DiscoveryRow("recommended_artists", "Artists", "recommended_artists", "artist", [{"id": "artist"}]),
        DiscoveryRow("quiet_picks", "Quiet Picks", "quiet_picks", "track", _row_items("quiet", 32)),
    ]
    accepted, reasons, status = evaluate_quality(
        rows=rows,
        taste=_taste(),
        home_tab_diagnostics={
            "accepted": True,
            "lane_item_counts": {"all": 12, "chill": 12, "workout": 12, "focus": 12, "mood": 12},
        },
    )
    assert accepted is False
    assert status == "build_failed"
    assert "missing_popular_radio" in reasons


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
                item={
                    "id": "new-album",
                    "title": "Neighbor Album",
                    "artist": "Neighbor",
                    "genre": "rock",
                    "source_authority": "verified_catalog",
                    "relation_type": "artist_neighbor",
                    "relation_strength": 0.82,
                },
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


def test_popular_radio_builds_artist_cards_with_playable_tracks() -> None:
    taste = _taste()
    taste.listened_artists = ["Band A", "Band C", "Dio", "Queen", "AC/DC"]
    candidates = []
    for artist in taste.listened_artists:
        for index in range(24):
            candidates.append(
                DiscoveryCandidate(
                    item={
                        "id": f"{artist}-{index}",
                        "title": f"{artist} Track {index}",
                        "artist": artist,
                        "genre": "rock",
                        "language": "english",
                        "region": "global",
                        "source_authority": "official",
                    },
                    source="profile_spine",
                    score=4.0,
                )
            )
    cards, diagnostics = build_popular_radio_cards(
        _radio_inventory_pools({
            "profile_spine": candidates,
            "artist_graph": [],
            "similarity": [],
            "ytmusic_home": [],
            "popularity": [],
            "discovery_universe": [],
        }),
        taste,
    )

    assert len(cards) >= 5
    assert diagnostics["card_count"] >= 5
    assert all(card["track_count"] >= 12 for card in cards[:5])
    assert all(card["item_type"] == "radio" if "item_type" in card else True for card in cards)
    assert {card["radio_mode"] for card in cards} == {"artist_catalog"}


def test_popular_radio_keeps_artist_and_mix_playlists_distinct() -> None:
    taste = _taste()
    taste.listened_artists = ["Queen", "AC/DC", "Dio"]
    candidates = []
    for artist in taste.listened_artists:
        for index in range(24):
            candidates.append(
                DiscoveryCandidate(
                    item={
                        "id": f"{artist}-catalog-{index}",
                        "title": f"{artist} Catalog {index}",
                        "artist": artist,
                        "genre": "rock",
                        "language": "english",
                        "region": "global",
                        "source_authority": "official",
                    },
                    source="profile_spine",
                    score=4.0,
                )
            )
    neighbors = []
    neighbor_artists = [
        "Scorpions",
        "Rainbow",
        "Black Sabbath",
        "Led Zeppelin",
        "Deep Purple",
        "Thin Lizzy",
    ]
    for anchor in taste.listened_artists:
        for index in range(60):
            artist = neighbor_artists[index % len(neighbor_artists)]
            neighbors.append(
                DiscoveryCandidate(
                    item={
                        "id": f"{anchor}-{artist}-neighbor-{index}",
                        "title": f"{artist} Neighbor {index}",
                        "artist": artist,
                        "genre": "rock",
                        "language": "english",
                        "region": "global",
                        "source_authority": "official",
                        "artist_neighborhood": True,
                        "related_to_artist": anchor,
                        "recommendation_path": "artist_neighbor",
                    },
                    source="artist_graph",
                    score=5.0,
                )
            )

    cards, _diagnostics = build_popular_radio_cards(
        _radio_inventory_pools({
            "profile_spine": candidates,
            "artist_graph": neighbors,
            "similarity": [],
            "ytmusic_home": [],
            "popularity": [],
            "discovery_universe": [],
        }),
        taste,
    )

    artist_cards = [card for card in cards if card["radio_mode"] == "artist_catalog"]
    mix_cards = [card for card in cards if card["radio_mode"] == "taste_mix"]
    discovery_cards = [card for card in cards if card["radio_mode"] == "discovery_mix"]
    assert artist_cards
    assert mix_cards
    assert discovery_cards
    for card in artist_cards:
        seed = card["seed_artist"]
        assert all(track["artist"] == seed for track in card["tracks"][:12])

    signatures = [
        {f"{track.get('title')}::{track.get('artist')}" for track in card["tracks"]}
        for card in cards
    ]
    for index, signature in enumerate(signatures):
        for other in signatures[index + 1 :]:
            overlap = len(signature & other) / max(len(signature), 1)
            assert overlap <= 0.35


def test_popular_radio_rejects_title_only_ufo_matches() -> None:
    taste = TasteProfile(
        user_scope_id="user-ufo",
        profile_key="profile-ufo",
        signal_tier="known",
        recent_tracks=[
            {"id": "seed-ufo", "title": "Doctor Doctor", "artist": "UFO", "genre": "hard rock"},
        ],
        artist_hints=["UFO"],
    )
    ufo_tracks = [
        DiscoveryCandidate(
            item={
                "id": f"ufo-real-{index}",
                "title": f"UFO Song {index}",
                "artist": "UFO",
                "genre": "hard rock",
                "language": "english",
                "region": "global",
                "source_authority": "official",
            },
            source="profile_spine",
            score=4.0,
        )
        for index in range(24)
    ]
    title_only = [
        DiscoveryCandidate(
            item={
                "id": f"title-only-{index}",
                "title": f"UFO Party {index}",
                "artist": f"Random Artist {index}",
                "genre": "dance pop",
                "language": "english",
                "region": "global",
                "source_authority": "official",
            },
            source="popularity",
            score=9.0,
        )
        for index in range(12)
    ]

    cards, _diagnostics = build_popular_radio_cards(
        _radio_inventory_pools({
            "profile_spine": ufo_tracks,
            "artist_graph": [],
            "similarity": [],
            "ytmusic_home": [],
            "popularity": title_only,
            "discovery_universe": title_only,
        }),
        taste,
    )

    ufo_card = next(card for card in cards if card["seed_artist"] == "UFO" and card["radio_mode"] == "artist_catalog")
    assert all(track["artist"] == "UFO" for track in ufo_card["tracks"][:12])


def test_popular_radio_rejects_off_profile_language_broad_tracks() -> None:
    taste = _taste()
    taste.listened_artists = ["Band A", "Band C", "Queen", "Dio", "AC/DC"]
    safe_tracks = []
    off_profile_tracks = []
    for artist in taste.listened_artists:
        for index in range(24):
            safe_tracks.append(
                DiscoveryCandidate(
                    item={
                        "id": f"safe-{artist}-{index}",
                        "title": f"{artist} Safe {index}",
                        "artist": artist,
                        "genre": "rock",
                        "language": "english",
                        "language_confidence": 0.9,
                        "region": "global",
                        "region_confidence": 0.9,
                        "source_authority": "official",
                    },
                    source="profile_spine",
                    score=4.0,
                )
            )
        off_profile_tracks.append(
            DiscoveryCandidate(
                item={
                    "id": f"off-profile-{artist}",
                    "title": f"{artist} Hindi Trend",
                    "artist": f"Off Profile {artist}",
                    "genre": "pop",
                    "language": "hindi",
                    "language_confidence": 0.9,
                    "region": "india",
                    "region_confidence": 0.9,
                    "source_authority": "official",
                },
                source="popularity",
                score=9.0,
            )
        )

    cards, diagnostics = build_popular_radio_cards(
        _radio_inventory_pools({
            "profile_spine": safe_tracks,
            "artist_graph": [],
            "similarity": [],
            "ytmusic_home": [],
            "popularity": off_profile_tracks,
            "discovery_universe": off_profile_tracks,
        }),
        taste,
    )

    all_tracks = [track for card in cards for track in card["tracks"]]
    assert all(not str(track["id"]).startswith("off-profile-") for track in all_tracks)
    assert diagnostics["rejection_counts"]


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
            "genre": "rock",
            "description": "high energy rock classic",
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
    assert queen["catalog_feature_version"] == "catalog-feature-v2"
    assert queen["mood_axes"]["energy"] > 0.5
    assert tribute is not None and tribute["discovery_quality_penalty"] >= 3.0


def test_track_normalization_detects_transliterated_bollywood_language() -> None:
    track = normalize_track(
        _FeatureServer(),
        {
            "id": "bollywood-1",
            "title": "Kesariya Bollywood",
            "artist": "Arijit Singh",
        },
    )
    assert track is not None
    assert track["language"] == "hindi"
    assert track["region"] == "india"


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
    assert track["region"] == "gb"
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
    assert [track["id"] for track in tracks] == ["official-neighbor"]
    assert tracks[0]["compatibility_reason"] in {
        "trusted_profile_bridge",
        "trusted_language_region_match",
    }


def test_home_response_uses_single_lightweight_contract_shape() -> None:
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
                "mix",
                [
                    {
                        "id": "mix-a",
                        "title": "Mix A",
                        "tracks": [
                            {
                                "id": "a",
                                "title": "A",
                                "artist": "Artist",
                                "ranking_features": {"internal": True},
                            }
                        ],
                        "items": [
                            {
                                "id": "a",
                                "title": "A",
                                "artist": "Artist",
                                "ranking_features": {"internal": True},
                            }
                        ],
                    }
                ],
            )
        ],
        diagnostics={
            "engine": "discovery_engine",
            "home_tab_lanes": {
                "all": {
                    "tracks": [
                        {
                            "id": "a",
                            "title": "A",
                            "artist": "Artist",
                            "ranking_features": {"internal": True},
                        }
                    ]
                }
            },
        },
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
    assert "shelves" not in response
    assert "recommendations" not in response
    mix = response["rows"][0]["items"][0]
    assert mix["id"] == "mix-a"
    assert "items" not in mix
    assert mix["tracks"][0]["id"] == "a"
    assert "ranking_features" not in mix["tracks"][0]
    lane_track = response["diagnostics"]["home_tab_lanes"]["all"]["tracks"][0]
    assert "ranking_features" not in lane_track
    assert response["diagnostics"]["engine"] == "discovery_engine"


def test_background_build_guard_suppresses_inflight_and_recent_duplicate() -> None:
    service = DiscoveryService(object())
    fingerprint = "user-a:profile-a"
    try:
        assert service._claim_background_build(fingerprint) is True
        assert service._claim_background_build(fingerprint) is False
        service._release_background_build(fingerprint)
        assert service._claim_background_build(fingerprint) is False
        assert service._claim_background_build(fingerprint, urgent=True) is True
    finally:
        service._release_background_build(fingerprint)
        service._prepare_executor.shutdown(wait=False, cancel_futures=True)


def test_rotation_taste_excludes_visible_active_tracks() -> None:
    service = DiscoveryService(object())
    active = _contract_ready_artifact("rotation")
    try:
        rotated = service._rotation_taste(_taste(), active, reason="test_refresh")
    finally:
        service._prepare_executor.shutdown(wait=False, cancel_futures=True)

    assert rotated.force_refresh is True
    assert rotated.refresh_token == "user-a:profile-a::0:test_refresh"
    assert "rotation-today-0" in rotated.avoid_ids
    assert "rotation-because-0" in rotated.avoid_ids


def _contract_ready_artifact(prefix: str, *, extra_items: int = 0) -> DiscoveryArtifact:
    now = time.time()
    mixes = [
        {
            "id": f"{prefix}-mix-{index}",
            "title": f"Mix {index}",
            "tracks": _row_items(f"{prefix}-mix-{index}-track", 8),
        }
        for index in range(5)
    ]
    radios = [
        {
            "id": f"{prefix}-radio-{index}",
            "title": f"Radio {index}",
            "tracks": _row_items(f"{prefix}-radio-{index}-track", 24),
        }
        for index in range(8)
    ]
    rows = [
        DiscoveryRow("todays_pick", "Today's pick", "todays_pick", "track", _row_items(f"{prefix}-today", 6 + extra_items)),
        DiscoveryRow("featured_new_albums", "Featured albums", "featured_new_albums", "album", _row_items(f"{prefix}-album", 8 + extra_items)),
        DiscoveryRow("last_played", "Last played", "last_played", "track", _row_items(f"{prefix}-last", 8)),
        DiscoveryRow("frequently_listened", "Frequently listened", "frequently_listened", "track", _row_items(f"{prefix}-freq", 8)),
        DiscoveryRow("made_for_you", "Made for you", "made_for_you", "mix", mixes),
        DiscoveryRow("because_you_played", "Because you played", "because_you_played", "track", _row_items(f"{prefix}-because", 24 + extra_items)),
        DiscoveryRow(
            "popular_radio",
            "Popular Radio",
            "popular_radio",
            "radio",
            radios,
        ),
        DiscoveryRow("recommended_albums", "Recommended albums", "recommended_albums", "album", _row_items(f"{prefix}-recommended-album", 12)),
        DiscoveryRow("recommended_artists", "Artists", "recommended_artists", "artist", _row_items(f"{prefix}-artist", 10)),
        DiscoveryRow("quiet_picks", "Quiet Picks", "quiet_picks", "track", _row_items(f"{prefix}-quiet", 20)),
    ]
    tab_diagnostics = {
        "accepted": True,
        "lane_item_counts": {"all": 24, "chill": 16, "workout": 16, "focus": 16},
    }
    return DiscoveryArtifact(
        session_id=f"session-{prefix}",
        user_scope_id="user-a",
        profile_key="profile-a",
        generated_at=now,
        expires_at=now + 3600,
        rows=rows,
        diagnostics={
            "artifact_quality": "servable",
            "artifact_status": "servable",
            "home_tab_diagnostics": tab_diagnostics,
        },
        candidate_pool_counts={},
        provider_timings_ms={},
        home_tab_lanes={},
        accepted=True,
        artifact_source="fresh_build",
    )


def test_required_row_shortages_use_acquisition_domain_names() -> None:
    artifact = _contract_ready_artifact("shortage-domain")
    artifact.rows = [
        row
        for row in artifact.rows
        if row.kind not in {"made_for_you", "popular_radio"}
    ]
    taste = TasteProfile(
        user_scope_id="user-a",
        profile_key="profile-a",
        signal_tier="known",
        recent_tracks=[{"id": "seed", "artist": "Band A"}],
    )

    shortages = row_shortage_domains(rows=artifact.rows, taste=taste)

    assert "made_for_you_tracks" in shortages
    assert "popular_radio" not in shortages


def test_feed_state_v2_promotes_prepared_atomically() -> None:
    user_scope = f"feed-v2-{int(time.time() * 1000000)}"
    active = _contract_ready_artifact("active")
    active.user_scope_id = user_scope
    prepared = _contract_ready_artifact("prepared")
    prepared.user_scope_id = user_scope
    state = FeedState(
        user_scope_id=user_scope,
        active_feed=active,
        prepared_feed=prepared,
        active_version=3,
        prepared_base_version=3,
        profile_fingerprint="profile-a",
        generation_status="prepared",
    )
    try:
        assert save_feed_state(None, state) is True
        promoted = promote_prepared_feed(None, state)
        loaded = load_feed_state(None, user_scope)
    finally:
        invalidate_feed_state(None, user_scope)

    assert promoted is not None
    assert promoted.session_id == prepared.session_id
    assert loaded is not None
    assert loaded.active_version == 4
    assert loaded.prepared_feed is None


def test_promoting_prepared_feed_schedules_exactly_one_successor(monkeypatch) -> None:
    user_scope = f"feed-v2-successor-{int(time.time() * 1000000)}"
    active = _contract_ready_artifact("successor-active")
    prepared = _contract_ready_artifact("successor-prepared")
    active.user_scope_id = user_scope
    prepared.user_scope_id = user_scope
    state = FeedState(
        user_scope_id=user_scope,
        active_feed=active,
        prepared_feed=prepared,
        active_version=2,
        prepared_base_version=2,
        profile_fingerprint="profile-a",
        generation_status="prepared",
    )
    taste = TasteProfile(
        user_scope_id=user_scope,
        profile_key="profile-a",
        signal_tier="known",
        recent_tracks=[{"id": "seed", "artist": "Band A"}],
    )
    service = DiscoveryService(object())
    scheduled: list[str] = []
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_taste_profile",
        lambda _server, _req: taste,
    )
    monkeypatch.setattr(
        service,
        "_schedule_preparation",
        lambda _req, _taste, *, reason: scheduled.append(reason),
    )
    try:
        assert save_feed_state(None, state) is True
        response = service.recommend(
            SimpleNamespace(user_scope_id=user_scope, force_refresh=True),
            request_mode="full_feed",
        )
    finally:
        service._prepare_executor.shutdown(wait=False, cancel_futures=True)
        invalidate_feed_state(None, user_scope)

    assert response["feed_action"] == "promoted_prepared"
    assert scheduled == ["post_promotion"]


def test_missing_required_row_records_targeted_replenishment_without_build_failure(
    monkeypatch,
) -> None:
    user_scope = f"feed-v2-thin-inventory-{int(time.time() * 1000000)}"
    state = FeedState(user_scope_id=user_scope)
    taste = TasteProfile(
        user_scope_id=user_scope,
        profile_key="profile-a",
        signal_tier="known",
        recent_tracks=[{"id": "seed", "artist": "Band A"}],
    )
    thin = CandidateInventory(
        user_scope_id=user_scope,
        profile_fingerprint="profile-a",
        generated_at=time.time(),
        expires_at=time.time() + 3600,
        generation_id="thin-generation",
    )
    rejected = _contract_ready_artifact("rejected-thin")
    rejected.user_scope_id = user_scope
    rejected.rows = [row for row in rejected.rows if row.kind != "made_for_you"]
    rejected.accepted = False
    rejected.quality_reasons = ["missing_required_rows", "missing_made_for_you"]
    rejected.diagnostics["row_shortage_domains"] = ["made_for_you_tracks"]
    stored: list[CandidateInventory] = []
    service = DiscoveryService(object())
    monkeypatch.setattr(
        "auralis_backend.discovery.service.load_candidate_inventory",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.schedule_catalog_population",
        lambda *_args, **_kwargs: {"reason": "completed_inline", "completed": True},
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_candidate_inventory",
        lambda *_args, **_kwargs: thin,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.verify_materialized_supply",
        lambda _server, supply, _taste, **_kwargs: supply,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.store_candidate_inventory",
        lambda _server, inventory, **_kwargs: stored.append(inventory) or True,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.candidate_inventory_coverage",
        lambda *_args, **_kwargs: {
            "ready": True,
            "actual": {"unique_tracks": 120},
            "minimums": {},
            "failed_contracts": [],
        },
    )
    monkeypatch.setattr(service, "_build_artifact", lambda *_args, **_kwargs: rejected)
    try:
        assert save_feed_state(None, state) is True
        service._schedule_preparation(
            SimpleNamespace(),
            taste,
            reason="inventory_replenish",
        )
        service._prepare_executor.shutdown(wait=True)
        loaded = load_feed_state(None, user_scope)
    finally:
        invalidate_feed_state(None, user_scope)

    assert [inventory.generation_id for inventory in stored] == [
        "thin-generation",
        "thin-generation",
    ]
    assert stored[-1].is_ready is False
    assert stored[-1].acquisition_ledger["failed_domains"] == [
        "made_for_you_tracks"
    ]
    assert loaded is not None
    assert loaded.active_feed is None
    assert loaded.generation_status == "inventory_building"
    assert loaded.dirty_reasons == ["row_shortage:made_for_you_tracks"]


def test_missing_required_row_runs_one_immediate_targeted_refill(monkeypatch) -> None:
    user_scope = f"feed-row-refill-{int(time.time() * 1000000)}"
    state = FeedState(user_scope_id=user_scope)
    taste = TasteProfile(
        user_scope_id=user_scope,
        profile_key="profile-a",
        signal_tier="known",
        recent_tracks=[{"id": "seed", "artist": "Band A"}],
    )
    inventory = CandidateInventory(
        user_scope_id=user_scope,
        profile_fingerprint="profile-a",
        generated_at=time.time(),
        expires_at=time.time() + 3600,
        generation_id="refill-generation",
    )
    rejected = _contract_ready_artifact("refill-rejected")
    rejected.user_scope_id = user_scope
    rejected.accepted = False
    rejected.quality_reasons = ["missing_required_rows", "missing_made_for_you"]
    rejected.diagnostics["row_shortage_domains"] = ["made_for_you_tracks"]
    stored: dict[str, CandidateInventory | None] = {"inventory": None}
    planned_domains: list[list[str]] = []

    class ImmediateExecutor:
        @staticmethod
        def submit(callback):
            callback()
            return SimpleNamespace()

        @staticmethod
        def shutdown(*_args, **_kwargs):
            return None

    service = DiscoveryService(object())
    service._prepare_executor = ImmediateExecutor()
    monkeypatch.setattr(
        "auralis_backend.discovery.service.load_candidate_inventory",
        lambda *_args, **_kwargs: stored["inventory"],
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.store_candidate_inventory",
        lambda _server, value, **_kwargs: stored.__setitem__("inventory", value)
        or True,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.schedule_catalog_population",
        lambda *_args, **_kwargs: {"reason": "cached"},
    )

    def fake_plan(_taste, *, acquisition_ledger=None):
        planned_domains.append(
            list(dict(acquisition_ledger or {}).get("failed_domains") or [])
        )
        return SimpleNamespace()

    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_enrichment_plan",
        fake_plan,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.materialize_enrichment_plan",
        lambda *_args, **_kwargs: SimpleNamespace(diagnostics={}),
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.verify_materialized_supply",
        lambda _server, supply, _taste, **_kwargs: supply,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_candidate_inventory",
        lambda *_args, **_kwargs: inventory,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.candidate_inventory_coverage",
        lambda *_args, **_kwargs: {
            "ready": True,
            "actual": {"unique_tracks": 120},
            "minimums": {},
            "failed_contracts": [],
        },
    )
    monkeypatch.setattr(service, "_build_artifact", lambda *_args, **_kwargs: rejected)
    try:
        assert save_feed_state(None, state) is True
        service._schedule_preparation(SimpleNamespace(), taste, reason="initial_feed")
        loaded = load_feed_state(None, user_scope)
    finally:
        invalidate_feed_state(None, user_scope)

    assert planned_domains == [[], ["made_for_you_tracks"]]
    assert loaded is not None
    assert loaded.generation_status == "inventory_building"


def test_real_prepare_exception_remains_a_build_failure(monkeypatch) -> None:
    user_scope = f"feed-real-failure-{int(time.time() * 1000000)}"
    state = FeedState(
        user_scope_id=user_scope,
        generation_status="build_failed",
        dirty_reasons=["prepare_exception:RuntimeError"],
    )
    service = DiscoveryService(object())
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_taste_profile",
        lambda _server, _req: TasteProfile(
            user_scope_id=user_scope,
            profile_key="profile-a",
            signal_tier="known",
        ),
    )
    monkeypatch.setattr(service, "_schedule_preparation", lambda *_args, **_kwargs: None)
    try:
        assert save_feed_state(None, state) is True
        response = service.recommend(
            SimpleNamespace(user_scope_id=user_scope),
            request_mode="full_feed",
        )
    finally:
        service._prepare_executor.shutdown(wait=False, cancel_futures=True)
        invalidate_feed_state(None, user_scope)

    assert response["feed_action"] == "build_failed"




def test_existing_active_feed_launch_does_not_build_in_request_path(monkeypatch) -> None:
    user_scope = f"feed-v2-launch-{int(time.time() * 1000000)}"
    active = _contract_ready_artifact("launch-active")
    active.user_scope_id = user_scope
    state = FeedState(
        user_scope_id=user_scope,
        active_feed=active,
        active_version=7,
        profile_fingerprint="profile-a",
        generation_status="ready",
    )
    service = DiscoveryService(object())
    scheduled: list[str] = []
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_taste_profile",
        lambda _server, _req: TasteProfile(
            user_scope_id=user_scope,
            profile_key="profile-a",
            signal_tier="known",
            recent_tracks=[{"id": "seed", "artist": "Band A"}],
        ),
    )
    monkeypatch.setattr(
        service,
        "_schedule_preparation",
        lambda _req, _taste, *, reason: scheduled.append(reason),
    )
    try:
        assert save_feed_state(None, state) is True
        response = service.recommend(
            SimpleNamespace(user_scope_id=user_scope),
            request_mode="full_feed",
        )
    finally:
        service._prepare_executor.shutdown(wait=False, cancel_futures=True)
        invalidate_feed_state(None, user_scope)

    assert response["feed_action"] == "served_active"
    assert response["feed_version"] == 7
    assert response["rows"]
    assert scheduled == ["launch_stale_inventory"]


def test_legacy_failed_initial_inventory_is_reported_as_preparing_while_resumed(
    monkeypatch,
) -> None:
    user_scope = f"feed-v2-retry-{int(time.time() * 1000000)}"
    state = FeedState(
        user_scope_id=user_scope,
        generation_status="build_failed",
        dirty_reasons=["candidate_inventory_exhausted:recommended_albums"],
    )
    service = DiscoveryService(object())
    scheduled: list[str] = []
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_taste_profile",
        lambda _server, _req: TasteProfile(
            user_scope_id=user_scope,
            profile_key="profile-a",
            signal_tier="known",
        ),
    )
    monkeypatch.setattr(
        service,
        "_schedule_preparation",
        lambda _req, _taste, *, reason: scheduled.append(reason),
    )
    try:
        assert save_feed_state(None, state) is True
        response = service.recommend(
            SimpleNamespace(user_scope_id=user_scope),
            request_mode="full_feed",
        )
    finally:
        service._prepare_executor.shutdown(wait=False, cancel_futures=True)
        invalidate_feed_state(None, user_scope)

    assert scheduled == ["initial_retry"]
    assert response["feed_action"] == "preparing_initial"


def test_existing_active_feed_without_prepared_successor_schedules_one(monkeypatch) -> None:
    user_scope = f"feed-v2-missing-successor-{int(time.time() * 1000000)}"
    active = _contract_ready_artifact("missing-successor")
    active.user_scope_id = user_scope
    state = FeedState(
        user_scope_id=user_scope,
        active_feed=active,
        active_version=4,
        profile_fingerprint="profile-a",
        active_inventory_generation="inventory-a",
        generation_status="ready",
    )
    taste = TasteProfile(
        user_scope_id=user_scope,
        profile_key="profile-a",
        signal_tier="known",
        recent_tracks=[{"id": "seed", "artist": "Band A"}],
    )
    inventory = CandidateInventory(
        user_scope_id=user_scope,
        profile_fingerprint="profile-a",
        generated_at=time.time(),
        expires_at=time.time() + 3600,
        generation_id="inventory-a",
    )
    service = DiscoveryService(object())
    scheduled: list[str] = []
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_taste_profile",
        lambda _server, _req: taste,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.load_candidate_inventory",
        lambda *_args, **_kwargs: inventory,
    )
    monkeypatch.setattr(
        service,
        "_schedule_preparation",
        lambda _req, _taste, *, reason: scheduled.append(reason),
    )
    try:
        assert save_feed_state(None, state) is True
        response = service.recommend(
            SimpleNamespace(user_scope_id=user_scope),
            request_mode="full_feed",
        )
    finally:
        service._prepare_executor.shutdown(wait=False, cancel_futures=True)
        invalidate_feed_state(None, user_scope)

    assert response["feed_action"] == "served_active"
    assert scheduled == ["launch_missing_successor"]


def test_search_return_keeps_active_visible_and_prepares_intent_successor(monkeypatch) -> None:
    user_scope = f"feed-v2-refresh-{int(time.time() * 1000000)}"
    active = _contract_ready_artifact("refresh-active")
    active.user_scope_id = user_scope
    candidate = _contract_ready_artifact("refresh-candidate")
    candidate.user_scope_id = user_scope
    state = FeedState(
        user_scope_id=user_scope,
        active_feed=active,
        active_version=4,
        profile_fingerprint="profile-a",
        generation_status="ready",
    )
    taste = TasteProfile(
        user_scope_id=user_scope,
        profile_key="profile-a",
        signal_tier="known",
        recent_tracks=[{"id": "seed", "artist": "Band A"}],
    )
    inventory = CandidateInventory(
        user_scope_id=user_scope,
        profile_fingerprint="profile-a",
        generated_at=time.time(),
        expires_at=time.time() + 3600,
    )
    service = DiscoveryService(object())
    scheduled_after_response: list[str] = []
    monkeypatch.setattr(
        "auralis_backend.discovery.service.build_taste_profile",
        lambda _server, _req: taste,
    )
    monkeypatch.setattr(
        "auralis_backend.discovery.service.load_candidate_inventory",
        lambda *_args, **_kwargs: inventory,
    )
    monkeypatch.setattr(service, "_build_artifact", lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(service, "_schedule_preparation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        service,
        "_schedule_preparation_after_response",
        lambda _req, *, reason, dedupe_key="": scheduled_after_response.append(
            reason
        ),
    )
    try:
        assert save_feed_state(None, state) is True
        response = service.recommend(
            SimpleNamespace(user_scope_id=user_scope, force_refresh=True),
            request_mode="full_feed",
        )
        refreshed = load_feed_state(None, user_scope)
        search_response = service.recommend(
            SimpleNamespace(user_scope_id=user_scope, session_intent=True),
            request_mode="full_feed",
        )
        loaded = load_feed_state(None, user_scope)
    finally:
        service._prepare_executor.shutdown(wait=False, cancel_futures=True)
        invalidate_feed_state(None, user_scope)

    assert response["feed_action"] == "built_and_promoted"
    assert response["feed_version"] == 5
    assert refreshed is not None
    assert refreshed.active_feed is not None
    assert refreshed.active_feed.session_id == candidate.session_id
    assert search_response["feed_action"] == "served_active"
    assert search_response["feed_version"] == 5
    assert loaded is not None
    assert loaded.active_feed is not None
    assert loaded.active_feed.session_id == candidate.session_id
    assert scheduled_after_response == ["search_session_intent"]


def test_feed_state_v2_rejects_stale_prepared_version() -> None:
    user_scope = f"feed-v2-stale-{int(time.time() * 1000000)}"
    active = _contract_ready_artifact("active")
    active.user_scope_id = user_scope
    prepared = _contract_ready_artifact("stale")
    prepared.user_scope_id = user_scope
    state = FeedState(
        user_scope_id=user_scope,
        active_feed=active,
        prepared_feed=prepared,
        active_version=5,
        prepared_base_version=4,
        profile_fingerprint="profile-a",
        generation_status="prepared",
    )
    try:
        assert save_feed_state(None, state) is True
        promoted = promote_prepared_feed(None, state)
        loaded = load_feed_state(None, user_scope)
    finally:
        invalidate_feed_state(None, user_scope)

    assert promoted is None
    assert loaded is not None
    assert loaded.active_version == 5
    assert loaded.prepared_feed is None
    assert loaded.generation_status == "stale_prepared_discarded"


def test_prepared_feed_accepts_popular_radio_artwork_improvement() -> None:
    user_scope = f"feed-v2-radio-art-{int(time.time() * 1000000)}"
    active = _contract_ready_artifact("radio-active")
    active.user_scope_id = user_scope
    prepared = _contract_ready_artifact("radio-prepared")
    prepared.user_scope_id = user_scope
    replacement = _contract_ready_artifact("radio-prepared")
    replacement.user_scope_id = user_scope
    replacement.session_id = "session-radio-art-repaired"
    radio = next(
        row for row in replacement.rows if row.kind == "popular_radio"
    )
    for index, item in enumerate(radio.items):
        item["thumbnail"] = f"https://images.example/radio-{index}.jpg"
    state = FeedState(
        user_scope_id=user_scope,
        active_feed=active,
        prepared_feed=prepared,
        active_version=4,
        prepared_base_version=4,
        prepared_inventory_generation="inventory-a",
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
            inventory_generation="inventory-a",
            rotation_epoch=2,
        )
        loaded = load_feed_state(None, user_scope)
    finally:
        invalidate_feed_state(None, user_scope)

    assert stored is not None
    assert loaded is not None
    assert loaded.prepared_feed is not None
    assert loaded.prepared_feed.session_id == "session-radio-art-repaired"
    reasons = list(
        loaded.prepared_feed.diagnostics.get("prepared_replacement_reasons")
        or []
    )
    assert "optional_quality_improved" in reasons


def test_prepared_feed_accepts_changed_radio_from_new_inventory() -> None:
    user_scope = f"feed-v2-radio-rotation-{int(time.time() * 1000000)}"
    active = _contract_ready_artifact("radio-active")
    active.user_scope_id = user_scope
    prepared = _contract_ready_artifact("radio-old")
    prepared.user_scope_id = user_scope
    replacement = _contract_ready_artifact("radio-new")
    replacement.user_scope_id = user_scope
    state = FeedState(
        user_scope_id=user_scope,
        active_feed=active,
        prepared_feed=prepared,
        active_version=6,
        prepared_base_version=6,
        prepared_inventory_generation="inventory-old",
        profile_fingerprint="profile-a",
        generation_status="prepared",
    )
    try:
        assert save_feed_state(None, state) is True
        stored = store_prepared_feed(
            None,
            state,
            replacement,
            expected_active_version=6,
            inventory_generation="inventory-new",
            rotation_epoch=3,
        )
        loaded = load_feed_state(None, user_scope)
    finally:
        invalidate_feed_state(None, user_scope)

    assert stored is not None
    assert loaded is not None
    assert loaded.prepared_feed is not None
    assert loaded.prepared_feed.session_id == replacement.session_id
    reasons = list(
        loaded.prepared_feed.diagnostics.get("prepared_replacement_reasons")
        or []
    )
    assert "new_inventory_optional_content_changed" in reasons


def test_feed_state_v2_stale_writer_uses_latest_persisted_version() -> None:
    user_scope = f"feed-v2-race-{int(time.time() * 1000000)}"
    original = _contract_ready_artifact("original")
    original.user_scope_id = user_scope
    state = FeedState(
        user_scope_id=user_scope,
        active_feed=original,
        active_version=2,
        profile_fingerprint="profile-a",
        generation_status="ready",
    )
    try:
        assert save_feed_state(None, state) is True
        stale_writer = load_feed_state(None, user_scope)
        current_writer = load_feed_state(None, user_scope)
        assert stale_writer is not None and current_writer is not None

        replacement = _contract_ready_artifact("replacement")
        replacement.user_scope_id = user_scope
        store_active_feed(
            None,
            current_writer,
            replacement,
            profile_fingerprint="profile-b",
        )

        prepared = _contract_ready_artifact("prepared-after-race")
        prepared.user_scope_id = user_scope
        stored = store_prepared_feed(
            None,
            stale_writer,
            prepared,
        )
        loaded = load_feed_state(None, user_scope)
    finally:
        invalidate_feed_state(None, user_scope)

    assert loaded is not None
    assert loaded.active_version == 3
    assert loaded.active_feed is not None
    assert loaded.active_feed.session_id == replacement.session_id
    assert stored is None
    assert loaded.prepared_feed is None
    assert loaded.prepared_base_version == 0
    assert loaded.profile_fingerprint == "profile-b"


def test_home_allocation_targets_discovery_and_caps_cross_row_reuse() -> None:
    taste = _taste()
    familiar = [
        {
            "id": f"familiar-{index}",
            "title": f"Familiar {index}",
            "artist": "Band A",
            "relation_type": "same_artist",
            "novelty_score": 0.0,
        }
        for index in range(8)
    ]
    discoveries = [
        {
            "id": f"discovery-{index}",
            "title": f"Discovery {index}",
            "artist": f"Neighbor {index}",
            "relation_type": "artist_neighbor",
            "novelty_score": 0.9,
        }
        for index in range(8)
    ]
    rows = [
        DiscoveryRow("made", "Made", "made_for_you_tracks", "track", [*familiar, *discoveries]),
        DiscoveryRow("quiet", "Quiet", "quiet_picks", "track", [*familiar, *discoveries]),
    ]

    allocated, diagnostics = allocate_home_rows(rows, taste)

    assert len(allocated[0].items) == 16
    assert diagnostics["discovery_ratio"] >= 0.4
    assert diagnostics["max_track_row_occurrence"] <= 2


def test_nested_mix_and_radio_partitions_cannot_starve_quiet_picks() -> None:
    shared = [
        {
            "id": f"shared-{index}",
            "title": f"Shared {index}",
            "artist": f"Artist {index}",
            "relation_type": "artist_neighbor",
            "novelty_score": 0.9,
        }
        for index in range(40)
    ]
    mix_unique = [
        {
            "id": f"mix-unique-{index}",
            "title": f"Mix Unique {index}",
            "artist": f"Mix Artist {index}",
            "relation_type": "artist_neighbor",
            "novelty_score": 0.9,
        }
        for index in range(32)
    ]
    radio_unique = [
        {
            "id": f"radio-unique-{index}",
            "title": f"Radio Unique {index}",
            "artist": f"Radio Artist {index}",
            "relation_type": "artist_neighbor",
            "novelty_score": 0.9,
        }
        for index in range(24)
    ]
    rows = [
        DiscoveryRow(
            "made_for_you",
            "Made for you",
            "made_for_you",
            "mix",
            [{"id": "mix-1", "tracks": [*shared[:8], *mix_unique], "items": [*shared[:8], *mix_unique]}],
        ),
        DiscoveryRow(
            "popular_radio",
            "Popular Radio",
            "popular_radio",
            "radio",
            [{"id": "radio-1", "tracks": [*shared[:8], *radio_unique], "items": [*shared[:8], *radio_unique]}],
        ),
        DiscoveryRow("quiet_picks", "Quiet Picks", "quiet_picks", "track", list(shared)),
    ]

    allocated, diagnostics = allocate_home_rows(rows, _taste())

    quiet = next(row for row in allocated if row.kind == "quiet_picks")
    made = next(row for row in allocated if row.kind == "made_for_you")
    radio = next(row for row in allocated if row.kind == "popular_radio")
    quiet_ids = {item["id"] for item in quiet.items}
    mix_visible_ids = {item["id"] for item in made.items[0]["tracks"][:8]}
    radio_visible_ids = {item["id"] for item in radio.items[0]["tracks"][:8]}
    assert len(quiet.items) == 40
    assert not (quiet_ids & radio_visible_ids)
    assert not (mix_visible_ids & radio_visible_ids)
    assert diagnostics["allocation_partitions"]["made_for_you_nested"] == 40
    assert diagnostics["allocation_partitions"]["popular_radio_nested"] == 32


def test_row_status_is_recomputed_from_post_allocation_payload() -> None:
    rows = [
        DiscoveryRow(
            "quiet_picks",
            "Quiet Picks",
            "quiet_picks",
            "track",
            _row_items("quiet-final", 3),
            meta={"quality_warnings": ["allocation_below_target"]},
        )
    ]
    final_rows, row_status = _finalize_allocated_rows(
        rows,
        {"quiet_picks": {"status": "emitted", "count": 64}},
    )

    assert len(final_rows[0].items) == 3
    assert row_status["quiet_picks"]["count"] == 3
    assert "below_min_items_after_allocation" in row_status["quiet_picks"]["warnings"]


def test_inventory_merge_preserves_previous_ready_coverage() -> None:
    now = time.time()
    previous = CandidateInventory(
        user_scope_id="user-a",
        profile_fingerprint="profile-a",
        generated_at=now - 10,
        expires_at=now + 3600,
        generation_id="previous",
        pools={
            "similarity": [
                DiscoveryCandidate(item={"id": "old", "title": "Old"}, source="similarity")
            ]
        },
    )
    current = CandidateInventory(
        user_scope_id="user-a",
        profile_fingerprint="profile-a",
        generated_at=now,
        expires_at=now + 3600,
        generation_id="current",
        pools={
            "similarity": [
                DiscoveryCandidate(item={"id": "new", "title": "New"}, source="similarity")
            ]
        },
    )

    merged = merge_candidate_inventories(current, previous)

    assert [candidate.item["id"] for candidate in merged.pools["similarity"]] == ["new", "old"]
    assert merged.base_generation_id == "previous"


def test_search_intent_delta_is_injected_into_ranked_inventory_pools() -> None:
    now = time.time()
    inventory = CandidateInventory(
        user_scope_id="user-a",
        profile_fingerprint="profile-a",
        generated_at=now,
        expires_at=now + 3600,
        generation_id="ready",
    )
    patched = apply_inventory_intent_delta(
        inventory,
        {
            "version": 7,
            "entries": [
                {
                    "entity_type": "track",
                    "item": {
                        "id": "layla",
                        "title": "Layla",
                        "artist": "Derek and the Dominos",
                        "similar_tracks": [{"id": "bell-bottom-blues", "title": "Bell Bottom Blues"}],
                    },
                }
            ],
        },
    )

    assert patched.intent_version == 7
    assert inventory.intent_version == 0
    assert inventory.pools == {}
    assert patched.pools["profile_spine"][0].item["id"] == "layla"
    assert any(candidate.item.get("id") == "bell-bottom-blues" for candidate in patched.pools["similarity"])


def test_feed_signature_includes_nested_track_identity() -> None:
    service = DiscoveryService(object())
    first = _contract_ready_artifact("nested")
    second = _contract_ready_artifact("nested")
    first.rows[4].items = [{"id": "mix-card", "tracks": [{"id": "nested-a"}]}]
    second.rows[4].items = [{"id": "mix-card", "tracks": [{"id": "nested-b"}]}]
    try:
        assert service._artifact_signature(first) != service._artifact_signature(second)
        assert "made_for_you" in service._changed_row_kinds(first, second)
    finally:
        service._prepare_executor.shutdown(wait=False, cancel_futures=True)


def test_radio_neighbor_is_bound_to_its_declared_anchor() -> None:
    candidate = DiscoveryCandidate(
        item={
            "id": "neighbor-track",
            "title": "Neighbor Track",
            "artist": "Neighbor Band",
            "related_to_artist": "Band A",
            "artist_neighborhood": True,
        },
        source="artist_graph",
        reasons=["artist_neighbor"],
    )

    assert _radio_relation_for_candidate(candidate, _taste(), "band a") == "artist_neighbor"
    assert _radio_relation_for_candidate(candidate, _taste(), "band c") == ""


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
    assert all(selected_by_source.get(source, 0) <= quota for source, quota in TRACK_POOL_QUOTAS.items())
    assert selected_by_source.get("history", 0) == 0
    assert selected_by_source.get("profile_spine", 0) == 0
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

    assert 5 <= len(mixes) <= 12
    assert diagnostics["mix_count"] == len(mixes)
    assert diagnostics["dynamic_mix_count"] >= 1
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
    assert "recommendations" not in page
    assert "shelves" not in page
    assert len(page["row"]["items"]) == 12
    assert page["next_offset"] == 20
    assert page["has_more"] is True
