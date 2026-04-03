from __future__ import annotations

from collections import Counter
import re
from typing import Any, Dict

from ..domain.collaborative import track_scores as collaborative_track_scores
from .feature_layer import build_catalog_feature_profile, candidate_catalog_alignment, script_bucket
from .row_registry import ranking_settings


_SCRIPT_PATTERNS = {
    "devanagari": re.compile(r"[\u0900-\u097F]"),
    "arabic": re.compile(r"[\u0600-\u06FF]"),
    "cyrillic": re.compile(r"[\u0400-\u04FF]"),
    "han": re.compile(r"[\u4E00-\u9FFF]"),
    "kana": re.compile(r"[\u3040-\u30FF]"),
    "hangul": re.compile(r"[\uAC00-\uD7AF]"),
}


def _script_bucket(text: str) -> str:
    return script_bucket(text)


def _profile_language_guardrail(server: Any, profile: Dict[str, Any]) -> Dict[str, Any]:
    cached = profile.get("_language_guardrail")
    if isinstance(cached, dict) and cached:
        return cached
    feature_profile = build_catalog_feature_profile(server, profile)

    guardrail = {
        "dominant_script": feature_profile.get("dominant_script") or "latin",
        "supported_scripts": set(feature_profile.get("supported_scripts") or {"latin"}),
        "supported_languages": set(feature_profile.get("supported_languages") or {"english"}),
        "affinity_artists": set(feature_profile.get("affinity_artists") or set()),
        "affinity_albums": set(feature_profile.get("affinity_albums") or set()),
    }
    profile["_language_guardrail"] = guardrail
    return guardrail


def _artist_cluster_profile(server: Any, profile: Dict[str, Any]) -> Dict[str, Any]:
    cached = profile.get("_artist_cluster_profile")
    if isinstance(cached, dict) and cached:
        return cached
    feature_profile = build_catalog_feature_profile(server, profile)
    cluster_profile = {
        "dominant_artist_keys": set(feature_profile.get("dominant_artist_keys") or set()),
        "affinity_artist_keys": set(feature_profile.get("affinity_artists") or set()),
        "peer_artist_keys": set(feature_profile.get("peer_scene_keys") or set()),
    }
    profile["_artist_cluster_profile"] = cluster_profile
    return cluster_profile


def _is_exploratory_source(server: Any, source_name: str) -> bool:
    normalized = server._normalize_text(source_name or "")
    if not normalized:
        return False
    if "peer_artist_neighbors" in normalized:
        return False
    return any(
        token in normalized
        for token in (
            "exploration",
            "fallback",
            "artist neighbors",
            "artist_neighbors",
        )
    )


def _candidate_guardrail_penalties(
    server: Any,
    candidate: Dict[str, Any],
    profile: Dict[str, Any],
    similarities: Dict[str, float],
    collaborative_scores: Dict[str, float],
    catalog_alignment: Dict[str, Any],
) -> Dict[str, float]:
    track = dict(candidate.get("track") or {})
    guardrail = _profile_language_guardrail(server, profile)
    primary_source = str(candidate.get("primary_source") or "")
    source_names = {
        str(source_name)
        for source_name in (candidate.get("source_names") or [])
        if str(source_name)
    }
    if primary_source:
        source_names.add(primary_source)

    artist_key = server._normalize_text(
        track.get("channel") or track.get("artist") or track.get("author") or ""
    )
    album_key = server._normalize_text(track.get("album") or "")
    candidate_script = str(catalog_alignment.get("script_bucket") or "unknown")

    exploratory = any(_is_exploratory_source(server, source_name) for source_name in source_names)
    affinity_match = (
        (artist_key and artist_key in set(guardrail.get("affinity_artists") or []))
        or (album_key and album_key in set(guardrail.get("affinity_albums") or []))
    )
    strong_personal_signal = (
        max(
            float(similarities.get("taste") or 0.0),
            float(similarities.get("artist") or 0.0),
            float(similarities.get("anchor") or 0.0),
        ) >= 0.42
        or float(collaborative_scores.get("neighbor") or 0.0) >= 1.1
        or float(collaborative_scores.get("artist") or 0.0) >= 1.0
    )

    language_penalty = 0.0
    supported_scripts = set(guardrail.get("supported_scripts") or set())
    if (
        candidate_script not in {"unknown", guardrail.get("dominant_script")}
        and candidate_script not in supported_scripts
    ):
        language_penalty = 1.4 if exploratory else 0.85

    exploration_penalty = 0.0
    if exploratory and not affinity_match and not strong_personal_signal:
        exploration_penalty += 0.9
    elif (
        primary_source
        and "fallback" in server._normalize_text(primary_source)
        and not affinity_match
        and float(similarities.get("taste") or 0.0) < 0.25
        and float(collaborative_scores.get("neighbor") or 0.0) < 0.8
    ):
        exploration_penalty += 0.45

    scene_penalty = 0.0
    scene_affinity = float(catalog_alignment.get("scene_affinity") or 0.0)
    peer_scene_bonus = float(catalog_alignment.get("peer_scene_bonus") or 0.0)
    if (
        exploratory
        and not affinity_match
        and (scene_affinity + peer_scene_bonus) < 0.55
        and float(similarities.get("taste") or 0.0) < 0.34
        and float(collaborative_scores.get("neighbor") or 0.0) < 0.95
    ):
        scene_penalty += 0.7
    elif (
        "taste_fallback" in {server._normalize_text(name) for name in source_names}
        and (scene_affinity + peer_scene_bonus) < 0.45
        and float(similarities.get("long") or 0.0) < 0.25
    ):
        scene_penalty += 0.28

    era_penalty = 0.0
    if (
        catalog_alignment.get("era_bucket")
        and float(catalog_alignment.get("era_affinity") or 0.0) <= 0.0
        and float(catalog_alignment.get("adjacent_era_affinity") or 0.0) <= 0.0
        and not affinity_match
        and float(similarities.get("long") or 0.0) < 0.3
        and float(collaborative_scores.get("neighbor") or 0.0) < 1.0
    ):
        era_penalty = 0.32 if exploratory else 0.18

    type_penalty = 0.0
    if float(catalog_alignment.get("hard_avoid_type_penalty") or 0.0) > 0.0:
        type_penalty = 0.8 if exploratory else 0.45

    negative_feedback_penalty = min(
        float(catalog_alignment.get("negative_feedback_penalty") or 0.0),
        3.4,
    )

    return {
        "language": language_penalty,
        "exploration": exploration_penalty,
        "scene": scene_penalty,
        "era": era_penalty,
        "type": type_penalty,
        "negative_feedback": negative_feedback_penalty,
    }


def vector_similarities(server: Any, candidate_vector, profile: Dict[str, Any]) -> Dict[str, float]:
    vectors = profile.get("vectors") or {}
    if not candidate_vector:
        return {
            "taste": 0.0,
            "short": 0.0,
            "long": 0.0,
            "query": 0.0,
            "artist": 0.0,
            "anchor": 0.0,
        }
    return {
        "taste": server._assistant_cosine_similarity(candidate_vector, vectors.get("taste_vector") or []),
        "short": server._assistant_cosine_similarity(candidate_vector, vectors.get("short_term_vector") or []),
        "long": server._assistant_cosine_similarity(candidate_vector, vectors.get("long_term_vector") or []),
        "query": server._assistant_cosine_similarity(candidate_vector, vectors.get("query_vector") or []),
        "artist": server._assistant_cosine_similarity(candidate_vector, vectors.get("artist_vector") or []),
        "anchor": server._assistant_cosine_similarity(candidate_vector, vectors.get("anchor_vector") or []),
    }


def track_score(
    server: Any,
    candidate: Dict[str, Any],
    profile: Dict[str, Any],
    row_kind: str,
    candidate_vector=None,
) -> Dict[str, Any]:
    track = dict(candidate.get("track") or {})
    track_id = server._recommendation_trim_text(track.get("id"))
    artist_text = server._normalize_text(track.get("channel") or track.get("artist") or "")
    album_text = server._normalize_text(track.get("album") or "")
    similarities = vector_similarities(server, candidate_vector, profile)
    collaborative_scores = collaborative_track_scores(server, track, profile)
    experiment_variant = server._recommendation_trim_text(profile.get("experiment_variant")) or "control"
    collaborative_multiplier = 1.25 if experiment_variant == "collab_heavy" else 0.85
    source_score = float(
        candidate.get("source_score")
        or candidate.get("generator_score")
        or 0.0
    )
    source_votes = float(max(int(candidate.get("source_votes") or 1), 1))
    offline_bonus = 1.0 if track_id in (profile.get("offline_track_ids") or []) else 0.0
    library_bonus = 1.0 if track_id in (profile.get("library_track_ids") or []) else 0.0
    top_bonus = 1.0 if track_id in (profile.get("top_track_ids") or []) else 0.0
    recent_bonus = 1.0 if track_id in (profile.get("recent_track_ids") or []) else 0.0
    popularity = min(float(collaborative_scores["neighbor"]) / 5.0, 1.0)
    if top_bonus > 0.0:
        popularity = max(popularity, 1.0)
    novelty = 1.0
    if recent_bonus > 0.0:
        novelty = 0.05
    elif top_bonus > 0.0:
        novelty = 0.22
    elif library_bonus > 0.0:
        novelty = 0.5
    elif offline_bonus > 0.0:
        novelty = 0.38

    hint_match_boost = 0.0
    for index, artist_hint in enumerate((profile.get("artist_hints") or [])[:6]):
        normalized_hint = server._normalize_text(artist_hint)
        if normalized_hint and normalized_hint in artist_text:
            hint_match_boost += max(1.6 - (index * 0.18), 0.55)
    for index, album_hint in enumerate((profile.get("album_hints") or [])[:6]):
        normalized_hint = server._normalize_text(album_hint)
        if normalized_hint and normalized_hint in album_text:
            hint_match_boost += max(1.2 - (index * 0.16), 0.4)

    artist_cluster = _artist_cluster_profile(server, profile)
    peer_artist_bonus = 1.0 if artist_text and artist_text in set(artist_cluster.get("peer_artist_keys") or set()) else 0.0
    dominant_artist_penalty = (
        1.0
        if artist_text and artist_text in set(artist_cluster.get("dominant_artist_keys") or set())
        else 0.0
    )
    catalog_alignment = candidate_catalog_alignment(server, track, profile)

    ranking_features = {
        "source_score": source_score,
        "source_votes": source_votes,
        "taste_similarity": float(similarities["taste"]),
        "short_similarity": float(similarities["short"]),
        "long_similarity": float(similarities["long"]),
        "query_similarity": 0.0,
        "artist_similarity": float(similarities["artist"]),
        "anchor_similarity": float(similarities["anchor"]),
        "collab_latent": float(collaborative_scores["latent"]) * collaborative_multiplier,
        "collab_neighbor": min(float(collaborative_scores["neighbor"]), 5.0) * collaborative_multiplier,
        "collab_artist": min(float(collaborative_scores["artist"]), 6.0) * collaborative_multiplier,
        "offline_bonus": offline_bonus,
        "library_bonus": library_bonus,
        "top_bonus": top_bonus,
        "recent_bonus": recent_bonus,
        "popularity": popularity,
        "novelty": novelty,
        "peer_artist_bonus": peer_artist_bonus,
        "dominant_artist_penalty": dominant_artist_penalty,
        "scene_affinity": float(catalog_alignment.get("scene_affinity") or 0.0),
        "peer_scene_bonus": float(catalog_alignment.get("peer_scene_bonus") or 0.0),
        "genre_affinity": float(catalog_alignment.get("genre_affinity") or 0.0),
        "subgenre_affinity": float(catalog_alignment.get("subgenre_affinity") or 0.0),
        "era_affinity": float(catalog_alignment.get("era_affinity") or 0.0),
        "adjacent_era_affinity": float(catalog_alignment.get("adjacent_era_affinity") or 0.0),
        "language_affinity": float(catalog_alignment.get("language_affinity") or 0.0),
        "type_affinity": float(catalog_alignment.get("type_affinity") or 0.0),
        "script_affinity": float(catalog_alignment.get("script_affinity") or 0.0),
        "popularity_taste_fit": float(catalog_alignment.get("popularity_taste_fit") or 0.0),
        "novelty_tolerance_fit": float(catalog_alignment.get("novelty_tolerance_fit") or 0.0),
        "negative_feedback_penalty": float(catalog_alignment.get("negative_feedback_penalty") or 0.0),
        "same_title_ambiguity_penalty": float(catalog_alignment.get("same_title_ambiguity_penalty") or 0.0),
    }
    row_config = ranking_settings(row_kind)
    model_key = str(row_config.get("model_key") or "home_global_ranker_v4")
    model_version = server._ranking_model_version(model_key)
    model_defaults = server._ranking_defaults_for_model(model_key, server.HOME_GLOBAL_DEFAULT_WEIGHTS)
    base_score = server._ranking_score_features(
        model_key=model_key,
        defaults=model_defaults,
        features=ranking_features,
    )

    row_bias = hint_match_boost + float(row_config.get("base_bias") or 0.0)
    for similarity_name, weight in dict(row_config.get("similarity_weights") or {}).items():
        row_bias += float(similarities.get(similarity_name) or 0.0) * float(weight or 0.0)
    for feature_name, weight in dict(row_config.get("feature_weights") or {}).items():
        row_bias += float(ranking_features.get(feature_name) or 0.0) * float(weight or 0.0)
    for feature_name, bonus in dict(row_config.get("presence_bias") or {}).items():
        if float(ranking_features.get(feature_name) or 0.0) > 0.0:
            row_bias += float(bonus or 0.0)
    for feature_name, penalty in dict(row_config.get("absence_bias") or {}).items():
        if float(ranking_features.get(feature_name) or 0.0) <= 0.0:
            row_bias += float(penalty or 0.0)
    priority_biases = list(row_config.get("presence_priority_bias") or [])
    if priority_biases:
        matched_priority = False
        for feature_name, bias in priority_biases:
            if float(ranking_features.get(feature_name) or 0.0) > 0.0:
                row_bias += float(bias or 0.0)
                matched_priority = True
                break
        if not matched_priority:
            row_bias += float(row_config.get("presence_priority_default") or 0.0)

    guardrail_penalties = _candidate_guardrail_penalties(
        server,
        candidate,
        profile,
        similarities,
        collaborative_scores,
        catalog_alignment,
    )
    ranking_features["language_guardrail_penalty"] = float(
        guardrail_penalties["language"]
    )
    ranking_features["exploration_guardrail_penalty"] = float(
        guardrail_penalties["exploration"]
    )
    ranking_features["scene_guardrail_penalty"] = float(
        guardrail_penalties["scene"]
    )
    ranking_features["era_guardrail_penalty"] = float(
        guardrail_penalties["era"]
    )
    ranking_features["type_guardrail_penalty"] = float(
        guardrail_penalties["type"]
    )
    ranking_features["feedback_guardrail_penalty"] = float(
        guardrail_penalties["negative_feedback"]
    )

    score = (
        base_score
        + row_bias
        - float(guardrail_penalties["language"])
        - float(guardrail_penalties["exploration"])
        - float(guardrail_penalties["scene"])
        - float(guardrail_penalties["era"])
        - float(guardrail_penalties["type"])
        - float(guardrail_penalties["negative_feedback"])
    )
    return {
        "score": score,
        "base_score": base_score,
        "row_bias": row_bias,
        "ranking_features": ranking_features,
        "ml_similarities": {
            "taste": round(similarities["taste"], 4),
            "short": round(similarities["short"], 4),
            "long": round(similarities["long"], 4),
            "query": round(similarities["query"], 4),
            "artist": round(similarities["artist"], 4),
            "anchor": round(similarities["anchor"], 4),
            "collab_latent": round(collaborative_scores["latent"], 4),
            "collab_neighbor": round(collaborative_scores["neighbor"], 4),
            "collab_artist": round(collaborative_scores["artist"], 4),
            "base_score": round(base_score, 4),
            "row_bias": round(row_bias, 4),
            "language_guardrail_penalty": round(
                float(guardrail_penalties["language"]),
                4,
            ),
            "exploration_guardrail_penalty": round(
                float(guardrail_penalties["exploration"]),
                4,
            ),
            "scene_guardrail_penalty": round(
                float(guardrail_penalties["scene"]),
                4,
            ),
            "era_guardrail_penalty": round(
                float(guardrail_penalties["era"]),
                4,
            ),
            "type_guardrail_penalty": round(
                float(guardrail_penalties["type"]),
                4,
            ),
            "feedback_guardrail_penalty": round(
                float(guardrail_penalties["negative_feedback"]),
                4,
            ),
            "scene_affinity": round(float(catalog_alignment.get("scene_affinity") or 0.0), 4),
            "peer_scene_bonus": round(float(catalog_alignment.get("peer_scene_bonus") or 0.0), 4),
            "genre_affinity": round(float(catalog_alignment.get("genre_affinity") or 0.0), 4),
            "subgenre_affinity": round(float(catalog_alignment.get("subgenre_affinity") or 0.0), 4),
            "era_affinity": round(float(catalog_alignment.get("era_affinity") or 0.0), 4),
            "adjacent_era_affinity": round(float(catalog_alignment.get("adjacent_era_affinity") or 0.0), 4),
            "language_affinity": round(float(catalog_alignment.get("language_affinity") or 0.0), 4),
            "type_affinity": round(float(catalog_alignment.get("type_affinity") or 0.0), 4),
            "script_affinity": round(float(catalog_alignment.get("script_affinity") or 0.0), 4),
            "popularity_taste_fit": round(float(catalog_alignment.get("popularity_taste_fit") or 0.0), 4),
            "novelty_tolerance_fit": round(float(catalog_alignment.get("novelty_tolerance_fit") or 0.0), 4),
            "negative_feedback_penalty": round(float(catalog_alignment.get("negative_feedback_penalty") or 0.0), 4),
            "same_title_ambiguity_penalty": round(float(catalog_alignment.get("same_title_ambiguity_penalty") or 0.0), 4),
        },
        "model_key": model_key,
        "model_version": model_version,
    }


def quality_floor(row_kind: str) -> float:
    return float((ranking_settings(row_kind) or {}).get("quality_floor") or 1.5)


def min_items(row_kind: str) -> int:
    return int((ranking_settings(row_kind) or {}).get("min_items") or 3)


def max_same_artist(row_kind: str) -> int:
    return int((ranking_settings(row_kind) or {}).get("max_same_artist") or 2)


def is_query_derived_source(server: Any, source_name: str) -> bool:
    normalized = server._normalize_text(source_name or "")
    if not normalized:
        return False
    return any(
        token in normalized
        for token in (
            "query",
            "search",
            "semantic",
            "suggest",
        )
    )
