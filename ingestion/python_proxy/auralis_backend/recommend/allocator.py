from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence

from .row_registry import allocator_settings

ROW_ALLOCATOR_DEFAULTS_BY_KEY: Dict[str, Dict[str, float]] = {
    "home_row_allocator_continue_v1": {
        "pool_count": 0.9,
        "source_score_mean": 0.8,
        "anchor_pool": 2.5,
        "history_pool": 1.9,
        "recent_overlap": 1.7,
        "top_overlap": 0.5,
        "artist_affinity_ratio": 1.45,
        "album_affinity_ratio": 0.85,
        "familiarity_ratio": 0.75,
        "novelty_ratio": -0.45,
        "fallback_pool": -0.95,
        "profile_recent_depth": 0.55,
    },
    "home_row_allocator_because_v1": {
        "pool_count": 0.82,
        "source_score_mean": 0.75,
        "anchor_pool": 2.2,
        "collaborative_pool": 1.35,
        "artist_pool": 1.0,
        "same_artist_pool": 1.1,
        "scene_pool": 1.2,
        "genre_pool": 0.9,
        "era_pool": 0.8,
        "recent_overlap": 0.8,
        "artist_affinity_ratio": 1.5,
        "album_affinity_ratio": 0.7,
        "familiarity_ratio": 0.35,
        "novelty_ratio": 0.2,
        "fallback_pool": -0.55,
        "profile_collab_ready": 0.45,
    },
    "home_row_allocator_listeners_v1": {
        "pool_count": 0.78,
        "source_score_mean": 0.7,
        "collaborative_pool": 2.35,
        "artist_pool": 1.1,
        "scene_pool": 1.55,
        "genre_pool": 0.8,
        "era_pool": 0.45,
        "popularity_pool": 0.4,
        "artist_affinity_ratio": 1.25,
        "familiarity_ratio": 0.25,
        "novelty_ratio": 0.35,
        "fallback_pool": -0.6,
        "profile_collab_ready": 0.85,
    },
    "home_row_allocator_frequent_v1": {
        "pool_count": 0.74,
        "source_score_mean": 0.65,
        "history_pool": 2.6,
        "top_overlap": 1.9,
        "recent_overlap": 1.1,
        "library_overlap": 0.35,
        "artist_affinity_ratio": 1.1,
        "album_affinity_ratio": 1.0,
        "familiarity_ratio": 1.2,
        "novelty_ratio": -1.0,
        "fallback_pool": -1.45,
    },
    "home_row_allocator_rediscover_v1": {
        "pool_count": 0.8,
        "source_score_mean": 0.72,
        "rediscovery_pool": 2.3,
        "artist_pool": 0.95,
        "exploration_pool": 0.65,
        "scene_pool": 1.0,
        "genre_pool": 0.75,
        "era_pool": 0.95,
        "top_overlap": 0.95,
        "recent_overlap": -1.75,
        "artist_affinity_ratio": 1.2,
        "album_affinity_ratio": 0.7,
        "familiarity_ratio": 0.5,
        "novelty_ratio": 1.85,
        "fallback_pool": -0.4,
        "profile_long_term_depth": 0.55,
    },
    "home_row_allocator_deep_cuts_v1": {
        "pool_count": 0.76,
        "source_score_mean": 0.7,
        "artist_pool": 1.9,
        "anchor_pool": 0.7,
        "exploration_pool": 1.25,
        "scene_pool": 1.8,
        "genre_pool": 1.25,
        "era_pool": 1.3,
        "language_pool": 0.65,
        "popularity_pool": 0.75,
        "same_artist_pool": -0.15,
        "artist_affinity_ratio": 1.85,
        "album_affinity_ratio": 0.5,
        "novelty_ratio": 1.45,
        "recent_overlap": -1.0,
        "top_overlap": 0.6,
        "fallback_pool": -0.5,
    },
    "home_row_allocator_offline_v1": {
        "pool_count": 0.72,
        "source_score_mean": 0.68,
        "offline_pool": 2.4,
        "history_pool": 0.9,
        "library_overlap": 1.9,
        "offline_overlap": 1.9,
        "artist_affinity_ratio": 0.8,
        "album_affinity_ratio": 0.55,
        "familiarity_ratio": 1.1,
        "novelty_ratio": -0.55,
        "fallback_pool": -0.8,
        "profile_offline_depth": 0.75,
    },
    "home_row_allocator_trending_v1": {
        "pool_count": 0.8,
        "source_score_mean": 0.72,
        "collaborative_pool": 2.2,
        "artist_pool": 1.55,
        "anchor_pool": 0.85,
        "history_pool": 0.45,
        "exploration_pool": -0.15,
        "scene_pool": 1.7,
        "genre_pool": 1.35,
        "era_pool": 1.15,
        "language_pool": 1.1,
        "popularity_pool": 1.45,
        "same_artist_pool": -0.2,
        "artist_affinity_ratio": 1.3,
        "album_affinity_ratio": 0.5,
        "familiarity_ratio": 0.3,
        "novelty_ratio": 0.6,
        "fallback_pool": -0.9,
        "profile_collab_ready": 0.5,
    },
    "home_row_allocator_quiet_v1": {
        "pool_count": 0.82,
        "source_score_mean": 0.72,
        "collaborative_pool": 1.55,
        "artist_pool": 1.7,
        "anchor_pool": 1.15,
        "history_pool": 0.95,
        "scene_pool": 1.75,
        "genre_pool": 1.35,
        "era_pool": 1.15,
        "language_pool": 0.95,
        "popularity_pool": 0.7,
        "same_artist_pool": 0.15,
        "recent_overlap": -0.55,
        "top_overlap": 0.7,
        "artist_affinity_ratio": 1.95,
        "album_affinity_ratio": 1.0,
        "familiarity_ratio": 0.7,
        "novelty_ratio": 0.35,
        "fallback_pool": -0.75,
        "exploration_pool": -0.2,
    },
}


def build_profile_allocator_features(profile: Dict[str, Any]) -> Dict[str, float]:
    recent_track_ids = list(profile.get("recent_track_ids") or [])
    top_track_ids = list(profile.get("top_track_ids") or [])
    library_track_ids = list(profile.get("library_track_ids") or [])
    offline_track_ids = list(profile.get("offline_track_ids") or [])
    collaborative_ids = list(
        ((profile.get("collaborative") or {}).get("candidate_track_ids") or [])
    )
    top_artists = list(profile.get("top_artists") or [])
    artist_hints = list(profile.get("artist_hints") or [])
    return {
        "profile_recent_depth": min(len(recent_track_ids) / 8.0, 1.0),
        "profile_long_term_depth": min(len(top_track_ids) / 20.0, 1.0),
        "profile_library_depth": min(len(library_track_ids) / 24.0, 1.0),
        "profile_offline_depth": min(len(offline_track_ids) / 16.0, 1.0),
        "profile_collab_ready": 1.0 if collaborative_ids else 0.0,
        "profile_artist_depth": min((len(top_artists) + len(artist_hints)) / 8.0, 1.0),
        "profile_album_depth": min(
            (
                len(list(profile.get("top_albums") or []))
                + len(list(profile.get("album_hints") or []))
            )
            / 8.0,
            1.0,
        ),
    }


def summarize_pool_features(
    server: Any,
    pool_name: str,
    candidates: Sequence[Dict[str, Any]],
    *,
    profile: Dict[str, Any],
) -> Dict[str, float]:
    items = [candidate for candidate in candidates or [] if isinstance(candidate, dict)]
    if not items:
        return {
            "pool_count": 0.0,
            "unique_artist_ratio": 0.0,
            "source_score_mean": 0.0,
            "source_votes_mean": 0.0,
            "recent_overlap": 0.0,
            "top_overlap": 0.0,
            "library_overlap": 0.0,
            "offline_overlap": 0.0,
            "novelty_ratio": 0.0,
            "artist_affinity_ratio": 0.0,
            "album_affinity_ratio": 0.0,
            "familiarity_ratio": 0.0,
            "anchor_pool": 1.0 if "anchor" in pool_name else 0.0,
            "collaborative_pool": 1.0 if "collab" in pool_name else 0.0,
            "artist_pool": 1.0 if "artist" in pool_name else 0.0,
            "history_pool": 1.0 if "history" in pool_name else 0.0,
            "rediscovery_pool": 1.0 if "rediscovery" in pool_name else 0.0,
            "exploration_pool": 1.0 if "exploration" in pool_name else 0.0,
            "offline_pool": 1.0 if "offline" in pool_name else 0.0,
            "fallback_pool": 1.0 if "fallback" in pool_name else 0.0,
            "same_artist_pool": 1.0 if "same_artist" in pool_name else 0.0,
            "scene_pool": 1.0 if "scene" in pool_name else 0.0,
            "genre_pool": 1.0 if "genre" in pool_name else 0.0,
            "era_pool": 1.0 if "era" in pool_name else 0.0,
            "language_pool": 1.0 if "language" in pool_name else 0.0,
            "popularity_pool": 1.0 if "popularity" in pool_name else 0.0,
        }

    recent_track_ids = set(profile.get("recent_track_ids") or [])
    top_track_ids = set(profile.get("top_track_ids") or [])
    library_track_ids = set(profile.get("library_track_ids") or [])
    offline_track_ids = set(profile.get("offline_track_ids") or [])
    affinity_artist_keys = {
        server._normalize_text(name)
        for name in [
            *(profile.get("top_artists") or []),
            *(profile.get("artist_hints") or []),
            *(profile.get("listened_artists") or []),
        ]
        if server._normalize_text(name)
    }
    affinity_album_keys = {
        server._normalize_text(name)
        for name in [
            *(profile.get("top_albums") or []),
            *(profile.get("album_hints") or []),
        ]
        if server._normalize_text(name)
    }
    unique_artists = set()
    recent_hits = 0
    top_hits = 0
    library_hits = 0
    offline_hits = 0
    novel_hits = 0
    affinity_artist_hits = 0
    affinity_album_hits = 0
    source_score_total = 0.0
    source_votes_total = 0.0

    for candidate in items:
        track = candidate.get("track") if isinstance(candidate.get("track"), dict) else {}
        track_id = server._recommendation_trim_text(track.get("id"))
        artist_key = server._normalize_text(
            track.get("channel") or track.get("artist") or track.get("author") or ""
        )
        album_key = server._normalize_text(track.get("album") or "")
        if artist_key:
            unique_artists.add(artist_key)
        if artist_key and artist_key in affinity_artist_keys:
            affinity_artist_hits += 1
        if album_key and album_key in affinity_album_keys:
            affinity_album_hits += 1
        if track_id and track_id in recent_track_ids:
            recent_hits += 1
        if track_id and track_id in top_track_ids:
            top_hits += 1
        if track_id and track_id in library_track_ids:
            library_hits += 1
        if track_id and track_id in offline_track_ids:
            offline_hits += 1
        if track_id and track_id not in recent_track_ids and track_id not in top_track_ids:
            novel_hits += 1
        source_score_total += float(
            candidate.get("source_score")
            or candidate.get("generator_score")
            or 0.0
        )
        source_votes_total += float(candidate.get("source_votes") or 1.0)

    count = float(len(items))
    return {
        "pool_count": min(count / 24.0, 1.0),
        "unique_artist_ratio": min(len(unique_artists) / count, 1.0),
        "source_score_mean": min((source_score_total / count) / 5.0, 1.5),
        "source_votes_mean": min((source_votes_total / count) / 3.0, 1.5),
        "recent_overlap": recent_hits / count,
        "top_overlap": top_hits / count,
        "library_overlap": library_hits / count,
        "offline_overlap": offline_hits / count,
        "novelty_ratio": novel_hits / count,
        "artist_affinity_ratio": affinity_artist_hits / count,
        "album_affinity_ratio": affinity_album_hits / count,
        "familiarity_ratio": min(
            (recent_hits + top_hits + library_hits + offline_hits) / count,
            1.0,
        ),
        "anchor_pool": 1.0 if "anchor" in pool_name else 0.0,
        "collaborative_pool": 1.0 if "collab" in pool_name else 0.0,
        "artist_pool": 1.0 if "artist" in pool_name else 0.0,
        "history_pool": 1.0 if "history" in pool_name else 0.0,
        "rediscovery_pool": 1.0 if "rediscovery" in pool_name else 0.0,
        "exploration_pool": 1.0 if "exploration" in pool_name else 0.0,
        "offline_pool": 1.0 if "offline" in pool_name else 0.0,
        "fallback_pool": 1.0 if "fallback" in pool_name else 0.0,
        "same_artist_pool": 1.0 if "same_artist" in pool_name else 0.0,
        "scene_pool": 1.0 if "scene" in pool_name else 0.0,
        "genre_pool": 1.0 if "genre" in pool_name else 0.0,
        "era_pool": 1.0 if "era" in pool_name else 0.0,
        "language_pool": 1.0 if "language" in pool_name else 0.0,
        "popularity_pool": 1.0 if "popularity" in pool_name else 0.0,
    }


def summarize_snapshot_pool_features(
    server: Any,
    *,
    profile: Dict[str, Any],
    pools: Dict[str, Sequence[Dict[str, Any]]],
) -> Dict[str, Dict[str, float]]:
    return {
        pool_name: summarize_pool_features(
            server,
            pool_name,
            list(candidates or []),
            profile=profile,
        )
        for pool_name, candidates in dict(pools or {}).items()
    }


def _score_pool(
    server: Any,
    *,
    model_key: str,
    defaults: Dict[str, float],
    pool_features: Dict[str, float],
    profile_features: Dict[str, float],
) -> float:
    features = dict(pool_features)
    features.update(profile_features)
    return float(
        server._ranking_score_features(
            model_key=model_key,
            defaults=defaults,
            features=features,
        )
    )


def build_row_allocation_plan(
    server: Any,
    *,
    row_kind: str,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    limits = allocator_settings(row_kind)
    if not limits:
        return {}
    profile_features = dict(snapshot.get("profile_allocator_features") or {})
    if not profile_features:
        profile_features = build_profile_allocator_features(profile)
    pool_features = dict(snapshot.get("pool_allocator_features") or {})
    model_key = str(limits.get("model_key") or "home_row_allocator_default_v1")
    defaults = dict(ROW_ALLOCATOR_DEFAULTS_BY_KEY.get(model_key) or {})
    pool_scores = []
    for pool_name in dict(snapshot.get("pools") or {}).keys():
        features = dict(pool_features.get(pool_name) or {})
        if not features:
            features = summarize_pool_features(
                server,
                pool_name,
                list((snapshot.get("pools") or {}).get(pool_name) or []),
                profile=profile,
            )
        score = _score_pool(
            server,
            model_key=model_key,
            defaults=defaults,
            pool_features=features,
            profile_features=profile_features,
        )
        pool_scores.append((score, pool_name, features))
    pool_scores.sort(key=lambda item: item[0], reverse=True)

    selected_pool_names: List[str] = []
    for score, pool_name, _features in pool_scores:
        if score <= -1.25 and selected_pool_names:
            continue
        selected_pool_names.append(pool_name)
        if len(selected_pool_names) >= int(limits.get("max_pools") or 4):
            break
    if "taste_fallback" in dict(snapshot.get("pools") or {}) and "taste_fallback" not in selected_pool_names:
        if len(selected_pool_names) < int(limits.get("max_pools") or 4):
            selected_pool_names.append("taste_fallback")
        elif not selected_pool_names:
            selected_pool_names = ["taste_fallback"]

    source_pool_counts = {
        pool_name: len(list((snapshot.get("pools") or {}).get(pool_name) or []))
        for pool_name in selected_pool_names
    }
    personalized_count = sum(
        count
        for pool_name, count in source_pool_counts.items()
        if pool_name != "taste_fallback"
    )
    fallback_count = int(source_pool_counts.get("taste_fallback") or 0)
    if personalized_count and fallback_count:
        row_strategy = "hybrid"
        fallback_reason = "supplemental_fallback_pool"
    elif personalized_count:
        row_strategy = "personalized"
        fallback_reason = ""
    else:
        row_strategy = "fallback"
        fallback_reason = "sparse_personalized_signal"

    return {
        "model_key": model_key,
        "model_version": server._ranking_model_version(model_key),
        "candidate_limit": int(limits.get("candidate_limit") or 18),
        "pool_names": selected_pool_names,
        "pool_scores": [
            {
                "pool_name": pool_name,
                "score": round(float(score), 4),
                "count": int(len(list((snapshot.get("pools") or {}).get(pool_name) or []))),
            }
            for score, pool_name, _features in pool_scores
        ],
        "row_strategy": row_strategy,
        "fallback_reason": fallback_reason,
        "source_pool_counts": source_pool_counts,
    }
