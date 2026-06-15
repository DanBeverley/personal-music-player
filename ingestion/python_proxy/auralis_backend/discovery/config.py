from __future__ import annotations

from typing import Dict, List

from .schema import LaneRecipe, RowRecipe


ENGINE_NAME = "discovery_engine"
ENGINE_MODEL_VERSION = "discovery-engine:home_orchestrator_v4"

ARTIFACT_VERSION = "discovery_home_artifact_v4"
ARTIFACT_NAMESPACE = "discovery_home_artifact"
ARTIFACT_TTL_SECONDS = 60 * 60 * 24

PROVIDER_BUDGETS_MS: Dict[str, int] = {
    "history": 200,
    "similarity": 1500,
    "artist_graph": 3500,
    "genre_mood": 2500,
    "album": 2500,
    "freshness": 400,
    "popularity": 1200,
    "collaborative": 800,
}

TRACK_POOL_QUOTAS: Dict[str, int] = {
    "history": 64,
    "similarity": 120,
    "artist_graph": 96,
    "genre_mood": 120,
    "lane_chill": 40,
    "lane_workout": 40,
    "lane_focus": 40,
    "lane_mood": 40,
    "popularity": 96,
    "collaborative": 48,
}

DISCOVERY_UNIVERSE_TARGET = 420

ROW_RECIPES: Dict[str, RowRecipe] = {
    "todays_pick": RowRecipe(
        kind="todays_pick",
        title="Today's pick",
        item_type="track",
        launch_required=True,
        min_items=1,
        target_items=6,
        max_items=8,
        page_size=6,
        can_page=False,
        candidate_sources=("similarity", "artist_graph", "genre_mood", "popularity", "history"),
        ranking_intent="daily_pick",
    ),
    "featured_new_albums": RowRecipe(
        kind="featured_new_albums",
        title="Featured albums for you",
        item_type="album",
        launch_required=True,
        min_items=1,
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
        min_items=1,
        target_items=12,
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
        min_items=2,
        target_items=12,
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
        min_items=3,
        target_items=5,
        max_items=5,
        page_size=5,
        can_page=False,
        candidate_sources=("discovery_universe", "similarity", "artist_graph", "genre_mood", "collaborative", "popularity"),
        ranking_intent="personal_mix",
        row_style="mix_cards",
    ),
    "because_you_played": RowRecipe(
        kind="because_you_played",
        title="Because you played",
        item_type="track",
        launch_required=True,
        min_items=4,
        target_items=24,
        max_items=48,
        page_size=12,
        can_page=True,
        candidate_sources=("similarity", "artist_graph", "history", "popularity", "discovery_universe"),
        ranking_intent="anchor_recommendation",
    ),
    "trending_by_genre": RowRecipe(
        kind="trending_by_genre",
        title="Trending by genre",
        item_type="track",
        launch_required=True,
        min_items=6,
        target_items=32,
        max_items=64,
        page_size=12,
        can_page=True,
        candidate_sources=("genre_mood", "popularity", "similarity", "discovery_universe"),
        ranking_intent="genre_discovery",
        row_style="genre_tabs",
    ),
    "recommended_albums": RowRecipe(
        kind="recommended_albums",
        title="Recommended albums",
        item_type="album",
        launch_required=True,
        min_items=1,
        target_items=24,
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
        min_items=1,
        target_items=16,
        max_items=30,
        page_size=10,
        can_page=True,
        candidate_sources=("artist_graph", "similarity", "popularity", "discovery_universe"),
        ranking_intent="artist_discovery",
    ),
    "quiet_picks": RowRecipe(
        kind="quiet_picks",
        title="Quiet Picks",
        item_type="track",
        launch_required=False,
        min_items=8,
        target_items=64,
        max_items=96,
        page_size=20,
        can_page=True,
        candidate_sources=("similarity", "artist_graph", "genre_mood", "collaborative", "popularity", "discovery_universe"),
        ranking_intent="taste_discovery",
    ),
    "hidden_gems": RowRecipe(
        kind="hidden_gems",
        title="Hidden gems",
        item_type="track",
        launch_required=False,
        min_items=4,
        target_items=24,
        max_items=48,
        page_size=12,
        can_page=True,
        candidate_sources=("similarity", "artist_graph", "genre_mood", "popularity", "discovery_universe"),
        ranking_intent="novelty_discovery",
    ),
}

ROW_ORDER: List[str] = list(ROW_RECIPES.keys())

LANE_RECIPES: Dict[str, LaneRecipe] = {
    "all": LaneRecipe(
        lane_id="all",
        title="All",
        min_items=12,
        target_items=24,
        candidate_sources=("similarity", "artist_graph", "genre_mood", "collaborative", "popularity"),
    ),
    "chill": LaneRecipe(
        lane_id="chill",
        title="Chill",
        min_items=12,
        target_items=24,
        positive_hints=("acoustic", "soul", "rnb", "mellow", "soft", "jazz", "downtempo", "chill"),
        negative_hints=("metal", "thrash", "hardcore", "speed", "aggressive"),
        retrieval_queries=("chill mellow acoustic soul songs", "downtempo jazz rnb songs"),
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
        retrieval_queries=("workout upbeat rock dance songs", "high energy edm pop songs"),
        candidate_sources=("lane_workout",),
    ),
    "focus": LaneRecipe(
        lane_id="focus",
        title="Focus",
        min_items=12,
        target_items=24,
        positive_hints=("instrumental", "ambient", "piano", "lo-fi", "study", "soundtrack", "focus"),
        negative_hints=("live", "party", "metal", "punk", "hardcore"),
        retrieval_queries=("focus instrumental ambient study songs", "lo-fi piano soundtrack songs"),
        candidate_sources=("lane_focus",),
    ),
    "mood": LaneRecipe(
        lane_id="mood",
        title="Mood",
        min_items=12,
        target_items=24,
        positive_hints=("emotional", "atmospheric", "dream", "melancholy", "soul", "cinematic", "mood"),
        negative_hints=(),
        retrieval_queries=("emotional atmospheric cinematic songs", "dreamy melancholy soul songs"),
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
