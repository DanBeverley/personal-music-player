from __future__ import annotations

from typing import Dict

from ..config import get_backend_config
from ..storage.postgres import load_active_model_weights


HOME_GLOBAL_DEFAULT_WEIGHTS: Dict[str, float] = {
    "source_score": 0.36,
    "source_votes": 0.68,
    "taste_similarity": 5.4,
    "short_similarity": 1.8,
    "long_similarity": 1.2,
    "query_similarity": 1.8,
    "artist_similarity": 2.0,
    "anchor_similarity": 2.5,
    "collab_latent": 5.6,
    "collab_neighbor": 1.0,
    "collab_artist": 0.22,
    "offline_bonus": 1.1,
    "library_bonus": 0.65,
    "top_bonus": 1.8,
    "recent_bonus": 0.35,
    "popularity": 0.18,
    "novelty": 0.2,
    "scene_affinity": 1.05,
    "genre_affinity": 0.65,
    "subgenre_affinity": 0.42,
    "peer_scene_bonus": 0.72,
    "era_affinity": 0.62,
    "adjacent_era_affinity": 0.24,
    "language_affinity": 0.28,
    "type_affinity": 0.18,
    "script_affinity": 0.12,
    "popularity_taste_fit": 0.3,
    "novelty_tolerance_fit": 0.24,
    "negative_feedback_penalty": -2.2,
    "same_title_ambiguity_penalty": -0.9,
}

HOME_CONTINUE_DEFAULT_WEIGHTS: Dict[str, float] = {
    **HOME_GLOBAL_DEFAULT_WEIGHTS,
    "short_similarity": 2.35,
    "anchor_similarity": 2.9,
    "novelty": 0.12,
    "scene_affinity": 0.75,
    "genre_affinity": 0.5,
    "era_affinity": 0.32,
    "language_affinity": 0.12,
    "negative_feedback_penalty": -2.4,
}

HOME_BECAUSE_PLAYED_DEFAULT_WEIGHTS: Dict[str, float] = {
    **HOME_GLOBAL_DEFAULT_WEIGHTS,
    "anchor_similarity": 3.3,
    "artist_similarity": 2.4,
    "query_similarity": 1.1,
    "scene_affinity": 0.95,
    "genre_affinity": 0.6,
    "era_affinity": 0.35,
    "language_affinity": 0.14,
    "negative_feedback_penalty": -2.2,
}

HOME_QUIET_DEFAULT_WEIGHTS: Dict[str, float] = {
    **HOME_GLOBAL_DEFAULT_WEIGHTS,
    "query_similarity": 0.55,
    "taste_similarity": 6.1,
    "short_similarity": 2.1,
    "popularity": 0.08,
    "scene_affinity": 1.15,
    "genre_affinity": 0.72,
    "subgenre_affinity": 0.32,
    "peer_scene_bonus": 0.55,
    "era_affinity": 0.72,
    "adjacent_era_affinity": 0.28,
    "language_affinity": 0.32,
    "type_affinity": 0.35,
    "script_affinity": 0.18,
    "popularity_taste_fit": 0.18,
    "novelty_tolerance_fit": 0.34,
    "negative_feedback_penalty": -2.6,
}

HOME_TRENDING_DEFAULT_WEIGHTS: Dict[str, float] = {
    **HOME_GLOBAL_DEFAULT_WEIGHTS,
    "popularity": 0.42,
    "novelty": 0.38,
    "query_similarity": 0.42,
    "scene_affinity": 1.4,
    "genre_affinity": 0.84,
    "subgenre_affinity": 0.35,
    "peer_scene_bonus": 0.9,
    "era_affinity": 0.85,
    "adjacent_era_affinity": 0.42,
    "language_affinity": 0.34,
    "script_affinity": 0.18,
    "popularity_taste_fit": 0.55,
    "novelty_tolerance_fit": 0.42,
    "negative_feedback_penalty": -2.45,
}

HOME_DISCOVERY_DEFAULT_WEIGHTS: Dict[str, float] = {
    **HOME_GLOBAL_DEFAULT_WEIGHTS,
    "long_similarity": 1.55,
    "novelty": 0.48,
    "recent_bonus": 0.12,
    "scene_affinity": 1.1,
    "genre_affinity": 0.78,
    "subgenre_affinity": 0.38,
    "peer_scene_bonus": 0.72,
    "era_affinity": 0.78,
    "adjacent_era_affinity": 0.34,
    "language_affinity": 0.28,
    "type_affinity": 0.22,
    "script_affinity": 0.15,
    "popularity_taste_fit": 0.22,
    "novelty_tolerance_fit": 0.48,
    "negative_feedback_penalty": -2.35,
}

HOME_MODEL_DEFAULTS_BY_KEY: Dict[str, Dict[str, float]] = {
    "home_global_ranker_v4": HOME_GLOBAL_DEFAULT_WEIGHTS,
    "home_continue_ranker_v1": HOME_CONTINUE_DEFAULT_WEIGHTS,
    "home_because_played_ranker_v1": HOME_BECAUSE_PLAYED_DEFAULT_WEIGHTS,
    "home_quiet_ranker_v1": HOME_QUIET_DEFAULT_WEIGHTS,
    "home_trending_ranker_v1": HOME_TRENDING_DEFAULT_WEIGHTS,
    "home_discovery_ranker_v1": HOME_DISCOVERY_DEFAULT_WEIGHTS,
}

def defaults_for_model(model_key: str, fallback: Dict[str, float] | None = None) -> Dict[str, float]:
    if model_key in HOME_MODEL_DEFAULTS_BY_KEY:
        return dict(HOME_MODEL_DEFAULTS_BY_KEY[model_key])
    return dict(fallback or {})


def model_version(model_key: str) -> str:
    return f"{get_backend_config().model_namespace}:{model_key}"


def score_features(
    *,
    model_key: str,
    defaults: Dict[str, float],
    features: Dict[str, float],
) -> float:
    weights = load_active_model_weights(model_key, defaults)
    score = 0.0
    for key, value in features.items():
        score += float(weights.get(key, 0.0)) * float(value)
    return score
