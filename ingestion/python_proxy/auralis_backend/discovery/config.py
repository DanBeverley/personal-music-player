from __future__ import annotations

from typing import Dict, List

from .schema import LaneRecipe, RowRecipe


ENGINE_NAME = "discovery_engine"
ENGINE_MODEL_VERSION = "discovery-engine:home_orchestrator"

# Keep the current persisted identifier during the cutover.  There is one
# artifact implementation; changing this value would make the accepted feed
# from the previous build unreadable before its replacement is prepared.
ARTIFACT_VERSION = "discovery_home_artifact_v7"
ARTIFACT_TTL_SECONDS = 60 * 60 * 24

TRACK_POOL_QUOTAS: Dict[str, int] = {
    "history": 64,
    "profile_spine": 240,
    "similarity": 220,
    "artist_graph": 180,
    "genre_mood": 200,
    "lane_chill": 64,
    "lane_workout": 64,
    "lane_focus": 64,
    "lane_mood": 64,
    "ytmusic_home": 128,
    "popularity": 128,
    "collaborative": 80,
}

# Retained, canonical supply for several rotations and long row-detail pages.
# It is not a raw provider-result count or a visible-row publication gate.
DISCOVERY_UNIVERSE_TARGET = 640

# Radio cards can be published once they have a useful visible queue.  The
# worker keeps enriching accepted cards toward the deeper target in later
# cycles, but a 24-track minimum made otherwise healthy artist catalogs
# impossible to publish.
POPULAR_RADIO_CARD_MIN_TRACKS = 12
POPULAR_RADIO_CARD_TARGET_TRACKS = 24

ROW_RECIPES: Dict[str, RowRecipe] = {
    "todays_pick": RowRecipe(
        kind="todays_pick",
        title="Today's pick",
        item_type="track",
        launch_required=True,
        min_items=6,
        target_items=6,
        max_items=8,
        page_size=6,
        can_page=False,
        candidate_sources=("similarity", "artist_graph", "genre_mood", "profile_spine", "popularity"),
        ranking_intent="daily_pick",
    ),
    "featured_new_albums": RowRecipe(
        kind="featured_new_albums",
        title="Featured albums for you",
        item_type="album",
        launch_required=False,
        min_items=8,
        target_items=8,
        max_items=10,
        page_size=10,
        can_page=False,
        candidate_sources=(
            "fresh_or_recent_albums",
            "known_artist_albums",
            "adjacent_artist_albums",
            "genre_album_discovery",
            "classic_neighbor_albums",
            "album",
        ),
        ranking_intent="featured_album",
        row_style="hero_carousel",
    ),
    "last_played": RowRecipe(
        kind="last_played",
        title="Last played",
        item_type="track",
        launch_required=False,
        min_items=8,
        target_items=8,
        max_items=16,
        page_size=8,
        can_page=False,
        candidate_sources=("history",),
        ranking_intent="history_recent",
    ),
    "frequently_listened": RowRecipe(
        kind="frequently_listened",
        title="Frequently listened",
        item_type="track",
        launch_required=False,
        min_items=8,
        target_items=8,
        max_items=16,
        page_size=8,
        can_page=False,
        candidate_sources=("history",),
        ranking_intent="history_frequent",
    ),
    "made_for_you": RowRecipe(
        kind="made_for_you",
        title="Made for you",
        item_type="mix",
        launch_required=True,
        min_items=5,
        target_items=8,
        max_items=12,
        page_size=12,
        can_page=False,
        candidate_sources=("profile_spine", "similarity", "artist_graph", "collaborative", "popularity"),
        ranking_intent="personal_mix",
        row_style="mix_cards",
    ),
    "because_you_played": RowRecipe(
        kind="because_you_played",
        title="Because you played",
        item_type="track",
        launch_required=True,
        min_items=12,
        target_items=12,
        max_items=48,
        page_size=12,
        can_page=True,
        candidate_sources=("similarity", "artist_graph", "profile_spine"),
        ranking_intent="anchor_recommendation",
    ),
    "popular_radio": RowRecipe(
        kind="popular_radio",
        title="Popular Radio",
        item_type="radio",
        launch_required=False,
        min_items=8,
        target_items=12,
        max_items=12,
        page_size=12,
        can_page=True,
        candidate_sources=("profile_spine", "artist_graph", "similarity", "collaborative", "popularity"),
        ranking_intent="artist_radio",
        row_style="radio_cards",
    ),
    "recommended_albums": RowRecipe(
        kind="recommended_albums",
        title="Recommended albums",
        item_type="album",
        launch_required=False,
        min_items=12,
        target_items=12,
        max_items=48,
        page_size=12,
        can_page=True,
        candidate_sources=(
            "adjacent_artist_albums",
            "genre_album_discovery",
            "fresh_or_recent_albums",
            "classic_neighbor_albums",
            "known_artist_albums",
            "album",
        ),
        ranking_intent="album_discovery",
    ),
    "recommended_artists": RowRecipe(
        kind="recommended_artists",
        title="Recommended artists",
        item_type="artist",
        launch_required=False,
        min_items=10,
        target_items=10,
        max_items=48,
        page_size=10,
        can_page=True,
        candidate_sources=("profile_spine", "artist_graph", "similarity", "collaborative"),
        ranking_intent="artist_discovery",
    ),
    "quiet_picks": RowRecipe(
        kind="quiet_picks",
        title="Quiet Picks",
        item_type="track",
        launch_required=False,
        min_items=20,
        target_items=20,
        max_items=200,
        page_size=20,
        can_page=True,
        candidate_sources=(
            "genre_mood",
            "lane_chill",
            "lane_workout",
            "lane_focus",
            "lane_mood",
            "similarity",
            "artist_graph",
            "collaborative",
            "profile_spine",
            "popularity",
        ),
        ranking_intent="taste_discovery",
    ),
}

ROW_ORDER: List[str] = list(ROW_RECIPES.keys())

LANE_RECIPES: Dict[str, LaneRecipe] = {
    "all": LaneRecipe(
        lane_id="all",
        title="All",
        min_items=12,
        target_items=24,
        candidate_sources=("similarity", "artist_graph", "collaborative"),
    ),
    "chill": LaneRecipe(
        lane_id="chill",
        title="Chill",
        min_items=12,
        target_items=24,
        positive_hints=("acoustic", "soul", "rnb", "mellow", "soft", "jazz", "downtempo", "chill"),
        negative_hints=("metal", "thrash", "hardcore", "speed", "aggressive"),
        candidate_sources=("lane_chill",),
    ),
    "workout": LaneRecipe(
        lane_id="workout",
        title="Workout",
        min_items=12,
        target_items=24,
        positive_hints=("rock", "dance", "edm", "pop", "metal", "punk", "upbeat", "live", "energy"),
        negative_hints=("acoustic", "lullaby", "sleep", "piano ballad", "ambient", "slow", "quiet", "soft"),
        allow_acoustic=False,
        candidate_sources=("lane_workout",),
    ),
    "focus": LaneRecipe(
        lane_id="focus",
        title="Focus",
        min_items=12,
        target_items=24,
        positive_hints=("instrumental", "ambient", "piano", "lo-fi", "study", "soundtrack", "focus"),
        negative_hints=("live", "party", "metal", "punk", "hardcore"),
        candidate_sources=("lane_focus",),
    ),
    "mood": LaneRecipe(
        lane_id="mood",
        title="Mood",
        min_items=12,
        target_items=24,
        positive_hints=("emotional", "atmospheric", "dream", "melancholy", "soul", "cinematic", "mood"),
        negative_hints=(),
        candidate_sources=("lane_mood",),
    ),
}

LANE_ORDER: List[str] = list(LANE_RECIPES.keys())

PRODUCT_DEAD_ROWS = {
    "listeners_like_you",
    "offline_ready",
    "deep_cuts",
    "rediscover",
    "trending_for_you",
}

RETIRED_ROW_ALIASES = {
    "mixed_for_you": "made_for_you",
    "continue_listening": "last_played",
}
