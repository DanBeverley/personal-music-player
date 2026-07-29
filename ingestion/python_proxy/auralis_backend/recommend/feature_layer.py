from __future__ import annotations

from typing import Any, Dict, Iterable, List, Set

from .feature_store import get_album_feature, get_artist_feature, get_track_feature, script_bucket as _script_bucket
from .scene_graph_store import expand_profile_scene_graph
from .taste_runtime import build_taste_profile


_HARD_AVOID_TYPE_TAGS = {"cover"}


def script_bucket(text: str) -> str:
    return _script_bucket(text)


def _as_set(values: Iterable[str] | None) -> Set[str]:
    return {
        str(value or "").strip()
        for value in values or []
        if str(value or "").strip()
    }


def _normalize_weight_map(values: Dict[str, Any] | None) -> Dict[str, float]:
    output: Dict[str, float] = {}
    for key, value in dict(values or {}).items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        output[normalized_key] = float(value or 0.0)
    return output


def _merge_weight_maps(
    primary: Dict[str, Any] | None,
    secondary: Dict[str, Any] | None,
    *,
    secondary_scale: float = 0.6,
    limit: int = 12,
) -> Dict[str, float]:
    merged: Dict[str, float] = {}
    for key, value in _normalize_weight_map(primary).items():
        merged[key] = float(merged.get(key) or 0.0) + float(value or 0.0)
    for key, value in _normalize_weight_map(secondary).items():
        merged[key] = float(merged.get(key) or 0.0) + (float(value or 0.0) * float(secondary_scale))
    ordered = sorted(
        merged.items(),
        key=lambda item: (-float(item[1] or 0.0), str(item[0] or "")),
    )[: max(int(limit or 0), 1)]
    return {
        str(key): round(float(value or 0.0), 4)
        for key, value in ordered
        if str(key or "").strip() and float(value or 0.0) > 0.0
    }


def build_catalog_feature_profile(server: Any, profile: Dict[str, Any]) -> Dict[str, Any]:
    cached = profile.get("_catalog_feature_profile")
    if isinstance(cached, dict) and cached:
        return cached

    taste_profile = build_taste_profile(server, profile)
    affinity_artists = _as_set(taste_profile.get("affinity_artists") or [])
    dominant_artist_keys = _as_set(taste_profile.get("dominant_artist_keys") or [])
    scene_cluster_scores = _normalize_weight_map(taste_profile.get("scene_cluster_scores") or {})
    graph_context = expand_profile_scene_graph(
        server,
        artist_keys=[
            *sorted(affinity_artists),
            *sorted(dominant_artist_keys),
            *list(taste_profile.get("artist_neighborhood_preferences") or [])[:12],
        ],
        scene_cluster_ids=list(scene_cluster_scores.keys())[:12],
    )
    merged_scene_artist_scores = _normalize_weight_map(
        taste_profile.get("scene_artist_scores") or {}
    )
    for index, artist_key in enumerate(list(graph_context.get("scene_artist_keys") or [])[:32]):
        normalized_artist_key = str(artist_key or "").strip()
        if not normalized_artist_key:
            continue
        merged_scene_artist_scores[normalized_artist_key] = max(
            float(merged_scene_artist_scores.get(normalized_artist_key) or 0.0),
            round(max(0.08, 0.26 - (index * 0.006)), 4),
        )
    feature_profile = {
        "catalog_feature_version": taste_profile.get("catalog_feature_version") or "",
        "taste_profile_version": taste_profile.get("profile_version") or "",
        "scene_graph_version": taste_profile.get("scene_graph_version") or "",
        "feature_source": taste_profile.get("feature_source") or "stored_enriched",
        "affinity_artists": affinity_artists,
        "affinity_albums": _as_set(taste_profile.get("affinity_albums") or []),
        "affinity_titles": _as_set(taste_profile.get("affinity_titles") or []),
        "dominant_script": str(taste_profile.get("dominant_script") or "latin"),
        "supported_scripts": _as_set(taste_profile.get("supported_scripts") or ["latin"]),
        "dominant_language": str(taste_profile.get("dominant_language") or ""),
        "supported_languages": _as_set(
            taste_profile.get("accepted_languages")
            or taste_profile.get("supported_languages")
            or []
        ),
        "language_scores": _normalize_weight_map(taste_profile.get("language_scores") or {}),
        "dominant_region": str(taste_profile.get("dominant_region") or ""),
        "supported_regions": _as_set(
            taste_profile.get("accepted_regions")
            or taste_profile.get("supported_regions")
            or []
        ),
        "region_scores": _normalize_weight_map(taste_profile.get("region_scores") or {}),
        "dominant_era": str(taste_profile.get("dominant_era") or ""),
        "supported_eras": _as_set(taste_profile.get("supported_eras") or []),
        "supported_type_tags": _as_set(taste_profile.get("supported_type_tags") or []),
        "preferred_genres": _as_set(taste_profile.get("preferred_genres") or []),
        "preferred_subgenres": _as_set(taste_profile.get("preferred_subgenres") or []),
        "genre_scores": _merge_weight_maps(
            ((taste_profile.get("long_term") or {}).get("genres") or {}),
            ((taste_profile.get("session") or {}).get("genres") or {}),
            secondary_scale=0.72,
            limit=10,
        ),
        "subgenre_scores": _merge_weight_maps(
            ((taste_profile.get("long_term") or {}).get("subgenres") or {}),
            ((taste_profile.get("session") or {}).get("subgenres") or {}),
            secondary_scale=0.72,
            limit=10,
        ),
        "era_scores": _merge_weight_maps(
            ((taste_profile.get("long_term") or {}).get("eras") or {}),
            ((taste_profile.get("session") or {}).get("eras") or {}),
            secondary_scale=0.64,
            limit=10,
        ),
        "dominant_artist_keys": dominant_artist_keys,
        "scene_artist_scores": merged_scene_artist_scores,
        "scene_cluster_scores": scene_cluster_scores,
        "peer_scene_keys": _as_set(
            [
                *(taste_profile.get("peer_scene_keys") or []),
                *(graph_context.get("peer_artist_keys") or []),
            ]
        ),
        "artist_neighborhood_preferences": _as_set(
            [
                *(taste_profile.get("artist_neighborhood_preferences") or []),
                *(graph_context.get("peer_artist_keys") or []),
                *(graph_context.get("scene_artist_keys") or []),
            ]
        ),
        "album_title_artist_keys": {
            str(title_key): _as_set(artist_keys)
            for title_key, artist_keys in dict(taste_profile.get("album_title_artist_keys") or {}).items()
            if str(title_key or "").strip()
        },
        "title_artist_keys": {
            str(title_key): _as_set(artist_keys)
            for title_key, artist_keys in dict(taste_profile.get("title_artist_keys") or {}).items()
            if str(title_key or "").strip()
        },
        "negative_feedback": {
            str(feedback_type): _normalize_weight_map(entries)
            for feedback_type, entries in dict(
                (taste_profile.get("negative_feedback") or {}).get("by_type") or {}
            ).items()
        },
        "negative_feedback_count": int(
            (taste_profile.get("negative_feedback") or {}).get("count") or 0
        ),
        "novelty_tolerance": float(taste_profile.get("novelty_tolerance") or 0.0),
        "popularity_tolerance": float(taste_profile.get("popularity_tolerance") or 0.0),
        "album_depth_preference": float(taste_profile.get("album_depth_preference") or 0.0),
        "mood_profile": {
            str(name): float(value or 0.0)
            for name, value in dict(taste_profile.get("mood_profile") or {}).items()
        },
        "hard_avoid_type_tags": set(_HARD_AVOID_TYPE_TAGS),
    }
    profile["_catalog_feature_profile"] = feature_profile
    return feature_profile


def _era_year(bucket: str) -> int | None:
    text = str(bucket or "").strip()
    if len(text) != 5 or not text.endswith("s"):
        return None
    try:
        value = int(text[:4])
    except Exception:
        return None
    return value if 1900 <= value <= 2090 else None


def _era_affinities(candidate_era: str, supported_eras: Set[str], dominant_era: str) -> Dict[str, float]:
    candidate_text = str(candidate_era or "").strip()
    if not candidate_text:
        return {"era_affinity": 0.0, "adjacent_era_affinity": 0.0}
    if candidate_text and candidate_text == str(dominant_era or ""):
        return {"era_affinity": 1.0, "adjacent_era_affinity": 0.0}
    if candidate_text in set(supported_eras or set()):
        return {"era_affinity": 0.8, "adjacent_era_affinity": 0.0}
    candidate_year = _era_year(candidate_text)
    if candidate_year is None:
        return {"era_affinity": 0.0, "adjacent_era_affinity": 0.0}
    for supported_era in supported_eras or set():
        supported_year = _era_year(supported_era)
        if supported_year is None:
            continue
        if abs(candidate_year - supported_year) <= 10:
            return {"era_affinity": 0.0, "adjacent_era_affinity": 0.55}
    return {"era_affinity": 0.0, "adjacent_era_affinity": 0.0}


def _shared_alignment(
    *,
    feature: Dict[str, Any],
    profile_features: Dict[str, Any],
    track_id: str = "",
    include_duplicate_title_penalty: bool = False,
) -> Dict[str, Any]:
    primary_genre = str(feature.get("primary_genre") or "").strip()
    secondary_genres = _as_set(feature.get("secondary_genres") or [])
    subgenre = str(feature.get("subgenre") or "").strip()
    language = str(feature.get("language") or "").strip()
    script = str(feature.get("script") or "unknown").strip() or "unknown"
    era_bucket = str(feature.get("era_bucket") or "").strip()
    track_type_tags = _as_set(feature.get("track_type_tags") or [])
    artist_key = str(feature.get("artist_key") or "").strip()
    album_key = str(feature.get("album_key") or "").strip()
    title_key = str(feature.get("title_key") or "").strip()
    scene_cluster_ids = _as_set(feature.get("scene_cluster_ids") or [])
    dominant_scene_score = max(
        max((profile_features.get("scene_cluster_scores") or {"": 0.0}).values(), default=0.0),
        1e-6,
    )
    dominant_artist_score = max(
        max((profile_features.get("scene_artist_scores") or {"": 0.0}).values(), default=0.0),
        1e-6,
    )

    genre_affinity = 0.0
    preferred_genres = set(profile_features.get("preferred_genres") or set())
    if primary_genre and primary_genre in preferred_genres:
        genre_affinity = 1.0
    elif secondary_genres & preferred_genres:
        genre_affinity = 0.65

    subgenre_affinity = 1.0 if subgenre and subgenre in set(profile_features.get("preferred_subgenres") or set()) else 0.0

    scene_affinity = 0.0
    for cluster in scene_cluster_ids:
        score = float((profile_features.get("scene_cluster_scores") or {}).get(cluster) or 0.0)
        if score > 0.0:
            scene_affinity = max(scene_affinity, min(score / dominant_scene_score, 1.0))
    if artist_key:
        score = float((profile_features.get("scene_artist_scores") or {}).get(artist_key) or 0.0)
        if score > 0.0:
            scene_affinity = max(scene_affinity, min(score / dominant_artist_score, 1.0))

    peer_scene_bonus = 1.0 if artist_key and artist_key in set(profile_features.get("peer_scene_keys") or set()) else 0.0
    if not peer_scene_bonus and scene_cluster_ids & set(profile_features.get("scene_cluster_scores") or {}):
        peer_scene_bonus = 0.35 if scene_affinity >= 0.65 else 0.0

    era_values = _era_affinities(
        era_bucket,
        set(profile_features.get("supported_eras") or set()),
        str(profile_features.get("dominant_era") or ""),
    )

    type_affinity = 0.0
    supported_type_tags = set(profile_features.get("supported_type_tags") or set())
    if track_type_tags and supported_type_tags:
        overlap = track_type_tags & supported_type_tags
        if overlap:
            type_affinity = min(len(overlap) / max(len(track_type_tags), 1), 1.0)

    language_affinity = 0.0
    dominant_language = str(profile_features.get("dominant_language") or "")
    supported_languages = set(profile_features.get("supported_languages") or set())
    if language and language == dominant_language:
        language_affinity = 1.0
    elif language and language in supported_languages:
        language_affinity = 0.72
    elif not language or language == "unknown":
        language_affinity = 0.22

    script_affinity = 0.0
    dominant_script = str(profile_features.get("dominant_script") or "")
    supported_scripts = set(profile_features.get("supported_scripts") or set())
    if script == dominant_script:
        script_affinity = 1.0
    elif script in supported_scripts:
        script_affinity = 0.72
    elif script == "unknown":
        script_affinity = 0.2

    popularity_taste_fit = max(
        0.0,
        1.0 - abs(float(feature.get("popularity") or 0.0) - float(profile_features.get("popularity_tolerance") or 0.0)),
    )
    novelty_tolerance_fit = max(
        0.0,
        1.0 - abs(float(feature.get("freshness") or 0.0) - float(profile_features.get("novelty_tolerance") or 0.0)),
    )

    negative_feedback = dict(profile_features.get("negative_feedback") or {})
    duplicate_key = f"{title_key}|{artist_key}" if title_key and artist_key else ""
    negative_feedback_penalty = (
        (float((negative_feedback.get("exact_track") or {}).get(track_id) or 0.0) * 3.2)
        + (float((negative_feedback.get("duplicate_track") or {}).get(duplicate_key) or 0.0) * 2.0)
        + (float((negative_feedback.get("artist_cluster") or {}).get(artist_key) or 0.0) * 1.05)
        + (float((negative_feedback.get("genre_cluster") or {}).get(primary_genre) or 0.0) * 0.82)
        + (float((negative_feedback.get("subgenre_cluster") or {}).get(subgenre) or 0.0) * 0.62)
        + (float((negative_feedback.get("language_cluster") or {}).get(language) or 0.0) * 0.75)
        + (float((negative_feedback.get("script_cluster") or {}).get(script) or 0.0) * 0.45)
        + max(
            [
                float((negative_feedback.get("scene_cluster") or {}).get(cluster) or 0.0)
                for cluster in scene_cluster_ids
            ]
            or [0.0]
        )
    )

    same_title_ambiguity_penalty = 0.0
    if include_duplicate_title_penalty and title_key:
        allowed_artist_keys = set(
            (profile_features.get("album_title_artist_keys") or {}).get(title_key) or set()
        )
        if allowed_artist_keys and artist_key and artist_key not in allowed_artist_keys:
            if scene_affinity < 0.55 and genre_affinity < 0.55 and peer_scene_bonus <= 0.0:
                same_title_ambiguity_penalty = 1.65

    return {
        "artist_key": artist_key,
        "album_key": album_key,
        "title_key": title_key,
        "primary_genre": primary_genre,
        "secondary_genres": sorted(secondary_genres),
        "subgenre": subgenre,
        "language": language,
        "script_bucket": script,
        "region": str(feature.get("region") or "").strip(),
        "release_year": feature.get("release_year"),
        "era_bucket": era_bucket,
        "scene_cluster_ids": sorted(scene_cluster_ids),
        "type_tags": track_type_tags,
        "genre_affinity": float(genre_affinity),
        "subgenre_affinity": float(subgenre_affinity),
        "scene_affinity": float(scene_affinity),
        "peer_scene_bonus": float(peer_scene_bonus),
        "era_affinity": float(era_values["era_affinity"]),
        "adjacent_era_affinity": float(era_values["adjacent_era_affinity"]),
        "language_affinity": float(language_affinity),
        "script_affinity": float(script_affinity),
        "type_affinity": float(type_affinity),
        "popularity_taste_fit": float(popularity_taste_fit),
        "novelty_tolerance_fit": float(novelty_tolerance_fit),
        "negative_feedback_penalty": round(float(negative_feedback_penalty), 4),
        "same_title_ambiguity_penalty": float(same_title_ambiguity_penalty),
        "hard_avoid_type_penalty": (
            1.0
            if track_type_tags & set(profile_features.get("hard_avoid_type_tags") or set())
            and not (track_type_tags & supported_type_tags)
            else 0.0
        ),
        "feature_source": str(feature.get("source_kind") or feature.get("feature_source") or "stored_enriched"),
        "item_feature_summary": {
            "primary_genre": primary_genre,
            "subgenre": subgenre,
            "era_bucket": era_bucket,
            "language": language,
            "script": script,
            "scene_cluster_ids": sorted(scene_cluster_ids)[:4],
            "track_type_tags": sorted(track_type_tags)[:4],
            "feature_source": str(feature.get("source_kind") or feature.get("feature_source") or "stored_enriched"),
        },
    }


def candidate_catalog_features(server: Any, track: Dict[str, Any]) -> Dict[str, Any]:
    return get_track_feature(server, track)


def candidate_catalog_alignment(server: Any, track: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    feature = get_track_feature(server, track)
    profile_features = build_catalog_feature_profile(server, profile)
    return _shared_alignment(
        feature=feature,
        profile_features=profile_features,
        track_id=str(feature.get("track_id") or server._recommendation_trim_text((track or {}).get("id")) or ""),
    )


def artist_catalog_alignment(server: Any, artist: Dict[str, Any] | str, profile: Dict[str, Any]) -> Dict[str, Any]:
    feature = get_artist_feature(server, artist)
    profile_features = build_catalog_feature_profile(server, profile)
    return _shared_alignment(
        feature=feature,
        profile_features=profile_features,
    )


def album_catalog_alignment(server: Any, album: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    feature = get_album_feature(server, album)
    profile_features = build_catalog_feature_profile(server, profile)
    return _shared_alignment(
        feature=feature,
        profile_features=profile_features,
        include_duplicate_title_penalty=True,
    )
