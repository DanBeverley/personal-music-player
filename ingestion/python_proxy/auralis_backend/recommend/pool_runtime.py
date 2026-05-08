from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from .feature_layer import (
    build_catalog_feature_profile,
    candidate_catalog_alignment,
    script_bucket,
)
from .freshness_runtime import (
    recent_row_impression_track_ids as _recent_row_impression_track_ids,
)


def _prefiltered_pool_name(row_kind: str) -> str:
    if row_kind == "quiet_picks":
        return "quiet_prefiltered"
    if row_kind == "deep_cuts":
        return "deep_cuts_prefiltered"
    return ""


def _prefilter_pool_order(row_kind: str) -> Tuple[str, ...]:
    if row_kind == "quiet_picks":
        return (
            "peer_scene",
            "genre_subgenre",
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "collaborative",
            "era_neighbors",
            "language_safe",
            "popularity_taste",
            "rediscovery",
            "history_top",
            "history_recent",
            "same_artist",
            "offline_library",
            "taste_fallback",
            "exploration",
        )
    if row_kind == "deep_cuts":
        return (
            "peer_scene",
            "genre_subgenre",
            "era_neighbors",
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "rediscovery",
            "collaborative",
            "history_top",
            "history_recent",
            "popularity_taste",
            "exploration",
            "taste_fallback",
        )
    return tuple()


def _candidate_signature(server: Any, candidate: Dict[str, Any]) -> str:
    track = candidate.get("track") if isinstance(candidate.get("track"), dict) else candidate
    return server._recommendation_track_signature(track)


def _candidate_copy(candidate: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}
    track = candidate.get("track")
    payload = {
        "generator_name": candidate.get("generator_name") or "",
        "generator_score": float(candidate.get("generator_score") or 0.0),
        "reason": candidate.get("reason") or "",
        "source_score": float(candidate.get("source_score") or 0.0),
        "source_votes": int(candidate.get("source_votes") or 1),
    }
    if isinstance(track, dict):
        payload["track"] = dict(track)
    else:
        payload["track"] = {}
    return payload


def _trim_candidate_pool(
    server: Any,
    candidates: Sequence[Dict[str, Any]],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    trimmed: List[Dict[str, Any]] = []
    seen = set()
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        copied = _candidate_copy(candidate)
        signature = _candidate_signature(server, copied)
        if not signature or signature in seen:
            continue
        seen.add(signature)
        trimmed.append(copied)
        if len(trimmed) >= limit:
            break
    return trimmed


def _feature_pool_candidate(
    candidate: Dict[str, Any],
    *,
    pool_name: str,
    score: float,
    reason: str,
) -> Dict[str, Any]:
    payload = _candidate_copy(candidate)
    payload["generator_name"] = pool_name
    payload["generator_score"] = max(
        float(payload.get("generator_score") or 0.0),
        float(score or 0.0),
    )
    payload["source_score"] = max(
        float(payload.get("source_score") or 0.0),
        float(score or 0.0),
    )
    if not payload.get("reason"):
        payload["reason"] = reason
    return payload


def _merge_pool_order(
    preferred_pool_names: Sequence[str],
    allocator_pool_names: Sequence[str],
    available_pools: Dict[str, Any],
) -> List[str]:
    merged: List[str] = []
    available = set(dict(available_pools or {}).keys())
    for pool_name in [*list(preferred_pool_names or ()), *list(allocator_pool_names or ())]:
        normalized = str(pool_name or "").strip()
        if not normalized or normalized in merged or normalized not in available:
            continue
        merged.append(normalized)
    return merged


def _build_feature_aware_pools(
    server: Any,
    profile: Dict[str, Any],
    base_pools: Dict[str, Sequence[Dict[str, Any]]],
    *,
    pool_candidate_cap: int,
) -> Dict[str, List[Dict[str, Any]]]:
    candidate_universe: List[Dict[str, Any]] = []
    seen_signatures = set()
    for pool_name in (
        "collaborative",
        "artist_neighbors",
        "anchor_neighbors",
        "primary_anchor_neighbors",
        "history_recent",
        "history_top",
        "rediscovery",
        "taste_fallback",
        "exploration",
        "offline_library",
    ):
        for candidate in list((base_pools or {}).get(pool_name) or []):
            if not isinstance(candidate, dict):
                continue
            copied = _candidate_copy(candidate)
            signature = _candidate_signature(server, copied)
            if not signature or signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            candidate_universe.append(copied)
    if not candidate_universe:
        return {}

    feature_profile = build_catalog_feature_profile(server, profile)
    dominant_artist_keys = set(feature_profile.get("dominant_artist_keys") or set())
    affinity_artists = set(feature_profile.get("affinity_artists") or set())
    scored_buckets: Dict[str, List[tuple[float, Dict[str, Any]]]] = defaultdict(list)
    for candidate in candidate_universe:
        track = candidate.get("track") if isinstance(candidate.get("track"), dict) else {}
        if not track:
            continue
        alignment = candidate_catalog_alignment(server, track, profile)
        base_score = float(
            candidate.get("source_score")
            or candidate.get("generator_score")
            or 0.0
        )
        artist_key = server._normalize_text(alignment.get("artist_key") or "")
        if artist_key and artist_key in (dominant_artist_keys | affinity_artists):
            scored_buckets["same_artist"].append(
                (
                    1.9
                    + float(alignment.get("scene_affinity") or 0.0)
                    + float(alignment.get("genre_affinity") or 0.0)
                    + (base_score * 0.08),
                    _feature_pool_candidate(
                        candidate,
                        pool_name="same_artist",
                        score=max(base_score, 4.4),
                        reason="More from the artists anchoring your current taste.",
                    ),
                )
            )
        if (
            float(alignment.get("peer_scene_bonus") or 0.0) > 0.0
            or float(alignment.get("scene_affinity") or 0.0) >= 0.55
        ):
            scored_buckets["peer_scene"].append(
                (
                    1.2
                    + float(alignment.get("scene_affinity") or 0.0) * 1.4
                    + float(alignment.get("peer_scene_bonus") or 0.0) * 1.2
                    + (base_score * 0.06),
                    _feature_pool_candidate(
                        candidate,
                        pool_name="peer_scene",
                        score=max(base_score, 4.1),
                        reason="Coming from the same scene and neighboring artists you lean toward.",
                    ),
                )
            )
        if (
            float(alignment.get("genre_affinity") or 0.0) >= 0.65
            or float(alignment.get("subgenre_affinity") or 0.0) >= 0.55
        ):
            scored_buckets["genre_subgenre"].append(
                (
                    1.0
                    + float(alignment.get("genre_affinity") or 0.0) * 1.35
                    + float(alignment.get("subgenre_affinity") or 0.0) * 1.15
                    + (base_score * 0.05),
                    _feature_pool_candidate(
                        candidate,
                        pool_name="genre_subgenre",
                        score=max(base_score, 3.8),
                        reason="A genre and subgenre match for the music you keep returning to.",
                    ),
                )
            )
        if (
            float(alignment.get("era_affinity") or 0.0) > 0.0
            or float(alignment.get("adjacent_era_affinity") or 0.0) > 0.0
        ):
            scored_buckets["era_neighbors"].append(
                (
                    0.85
                    + float(alignment.get("era_affinity") or 0.0) * 1.4
                    + float(alignment.get("adjacent_era_affinity") or 0.0) * 0.9
                    + (base_score * 0.04),
                    _feature_pool_candidate(
                        candidate,
                        pool_name="era_neighbors",
                        score=max(base_score, 3.6),
                        reason="Released in the era your listening profile keeps orbiting around.",
                    ),
                )
            )
        if (
            float(alignment.get("language_affinity") or 0.0) >= 0.72
            and float(alignment.get("script_affinity") or 0.0) >= 0.72
        ):
            scored_buckets["language_safe"].append(
                (
                    0.72
                    + float(alignment.get("language_affinity") or 0.0) * 1.1
                    + float(alignment.get("script_affinity") or 0.0) * 0.9
                    + (base_score * 0.03),
                    _feature_pool_candidate(
                        candidate,
                        pool_name="language_safe",
                        score=max(base_score, 3.2),
                        reason="A language and script fit for the music you regularly engage with.",
                    ),
                )
            )
        if (
            float(alignment.get("popularity_taste_fit") or 0.0) >= 0.62
            and float(alignment.get("negative_feedback_penalty") or 0.0) < 0.9
        ):
            scored_buckets["popularity_taste"].append(
                (
                    0.7
                    + float(alignment.get("popularity_taste_fit") or 0.0) * 1.15
                    + float(alignment.get("scene_affinity") or 0.0) * 0.4
                    + float(alignment.get("genre_affinity") or 0.0) * 0.35
                    + (base_score * 0.05),
                    _feature_pool_candidate(
                        candidate,
                        pool_name="popularity_taste",
                        score=max(base_score, 3.5),
                        reason="Popular right now inside the lane your taste profile supports.",
                    ),
                )
            )

    feature_pools: Dict[str, List[Dict[str, Any]]] = {}
    for pool_name, scored_items in scored_buckets.items():
        scored_items.sort(key=lambda item: item[0], reverse=True)
        feature_pools[pool_name] = _trim_candidate_pool(
            server,
            [candidate for _score, candidate in scored_items],
            limit=pool_candidate_cap,
        )
    return feature_pools


def _quiet_primary_pool_order(
    snapshot: Dict[str, Any],
    allocation_plan: Dict[str, Any],
) -> List[str]:
    return _merge_pool_order(
        _prefilter_pool_order("quiet_picks"),
        allocation_plan.get("pool_names") or (),
        dict(snapshot.get("pools") or {}),
    )


def _track_list_to_candidates(
    server: Any,
    tracks: Iterable[Dict[str, Any]],
    *,
    generator_name: str,
    base_score: float,
    reason: str,
) -> List[Dict[str, Any]]:
    return server._recommendation_candidates_from_tracks(
        list(tracks or []),
        generator_name,
        float(base_score),
        reason,
    )


def _extend_pool(
    server: Any,
    pool: List[Dict[str, Any]],
    candidates: Iterable[Dict[str, Any]],
    *,
    limit: int,
) -> None:
    existing_signatures = {
        signature
        for signature in (
            _candidate_signature(server, candidate)
            for candidate in pool
        )
        if signature
    }
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        copied = _candidate_copy(candidate)
        signature = _candidate_signature(server, copied)
        if not signature or signature in existing_signatures:
            continue
        pool.append(copied)
        existing_signatures.add(signature)
        if len(pool) >= limit:
            break


def _combine_pools(
    server: Any,
    snapshot: Dict[str, Any],
    pool_names: Sequence[str],
    *,
    limit: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    pools = dict(snapshot.get("pools") or {})
    combined: List[Dict[str, Any]] = []
    source_pool_counts: Dict[str, int] = {}
    for pool_name in pool_names:
        pool = list(pools.get(pool_name) or [])
        source_pool_counts[pool_name] = len(pool)
        _extend_pool(
            server,
            combined,
            pool,
            limit=limit,
        )
        if len(combined) >= limit:
            break
    return combined[:limit], source_pool_counts


def _script_bucket(text: str) -> str:
    return script_bucket(text)


def _row_affinity_profile(server: Any, profile: Dict[str, Any]) -> Dict[str, Any]:
    cached = profile.get("_row_affinity_profile")
    if isinstance(cached, dict) and cached:
        return cached
    catalog_profile = build_catalog_feature_profile(server, profile)

    affinity = {
        "artists": set(catalog_profile.get("affinity_artists") or set()),
        "albums": set(catalog_profile.get("affinity_albums") or set()),
        "titles": set(catalog_profile.get("affinity_titles") or set()),
        "preferred_genres": set(catalog_profile.get("preferred_genres") or set()),
        "preferred_subgenres": set(catalog_profile.get("preferred_subgenres") or set()),
        "genre_scores": {
            str(key): float(value or 0.0)
            for key, value in dict(catalog_profile.get("genre_scores") or {}).items()
            if str(key or "").strip()
        },
        "subgenre_scores": {
            str(key): float(value or 0.0)
            for key, value in dict(catalog_profile.get("subgenre_scores") or {}).items()
            if str(key or "").strip()
        },
        "era_scores": {
            str(key): float(value or 0.0)
            for key, value in dict(catalog_profile.get("era_scores") or {}).items()
            if str(key or "").strip()
        },
        "dominant_script": catalog_profile.get("dominant_script") or "latin",
        "supported_scripts": set(catalog_profile.get("supported_scripts") or {"latin"}),
        "supported_languages": set(catalog_profile.get("supported_languages") or {"english"}),
        "supported_eras": set(catalog_profile.get("supported_eras") or set()),
        "dominant_era": catalog_profile.get("dominant_era") or "",
        "supported_type_tags": set(catalog_profile.get("supported_type_tags") or set()),
        "peer_scene_keys": set(catalog_profile.get("peer_scene_keys") or set()),
    }
    profile["_row_affinity_profile"] = affinity
    return affinity


def _row_candidate_evidence(
    server: Any,
    profile: Dict[str, Any],
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    affinity = _row_affinity_profile(server, profile)
    track = dict(candidate.get("track") or {})
    artist_key = server._normalize_text(
        track.get("channel") or track.get("artist") or track.get("author") or ""
    )
    album_key = server._normalize_text(track.get("album") or "")
    title_key = server._normalize_text(track.get("title") or "")
    source_names = {
        server._normalize_text(source_name)
        for source_name in [
            candidate.get("generator_name"),
            candidate.get("primary_source"),
            *(candidate.get("source_names") or []),
        ]
        if server._normalize_text(source_name)
    }
    track_text = " ".join(
        part
        for part in [
            server._recommendation_trim_text(track.get("title")),
            server._recommendation_trim_text(
                track.get("channel") or track.get("artist") or track.get("author")
            ),
            server._recommendation_trim_text(track.get("album")),
        ]
        if part
    )
    script_bucket_value = _script_bucket(track_text)
    artist_match = bool(artist_key and artist_key in set(affinity.get("artists") or []))
    album_match = bool(album_key and album_key in set(affinity.get("albums") or []))
    title_match = bool(title_key and title_key in set(affinity.get("titles") or []))
    affinity_score = 0.0
    if artist_match:
        affinity_score += 2.2
    if album_match:
        affinity_score += 1.35
    if title_match:
        affinity_score += 0.55

    trusted_source = any(
        token in source_name
        for source_name in source_names
        for token in (
            "collaborative",
            "peer_artist_neighbors",
            "anchor_neighbors",
            "primary_anchor_neighbors",
            "rediscovery",
            "history_",
            "offline_library",
        )
    )
    exploratory_source = any(
        token in source_name
        for source_name in source_names
        for token in (
            "exploration",
            "fallback",
        )
    )
    supported_script = (
        script_bucket_value == "unknown"
        or script_bucket_value == affinity.get("dominant_script")
        or script_bucket_value in set(affinity.get("supported_scripts") or set())
    )
    catalog_alignment = candidate_catalog_alignment(server, track, profile)
    catalog_score = (
        float(catalog_alignment.get("scene_affinity") or 0.0)
        + (float(catalog_alignment.get("peer_scene_bonus") or 0.0) * 0.85)
        + (float(catalog_alignment.get("genre_affinity") or 0.0) * 0.7)
        + (float(catalog_alignment.get("subgenre_affinity") or 0.0) * 0.45)
        + (float(catalog_alignment.get("era_affinity") or 0.0) * 0.7)
        + (float(catalog_alignment.get("adjacent_era_affinity") or 0.0) * 0.4)
        + (float(catalog_alignment.get("language_affinity") or 0.0) * 0.35)
        + (float(catalog_alignment.get("type_affinity") or 0.0) * 0.45)
        + (float(catalog_alignment.get("script_affinity") or 0.0) * 0.2)
    )
    return {
        "artist_match": artist_match,
        "album_match": album_match,
        "title_match": title_match,
        "affinity_score": affinity_score,
        "trusted_source": trusted_source,
        "exploratory_source": exploratory_source,
        "supported_script": supported_script,
        "script_bucket": script_bucket_value,
        "source_names": source_names,
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
        "catalog_score": float(catalog_score),
    }


def _post_filter_row_candidates(
    server: Any,
    row_kind: str,
    profile: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    *,
    relaxed: bool = False,
) -> List[Dict[str, Any]]:
    if row_kind not in {"quiet_picks", "deep_cuts", "trending_for_you", "rediscover"}:
        return list(candidates or [])
    recent_track_ids = set(profile.get("recent_track_ids") or [])
    recent_row_track_ids = _recent_row_impression_track_ids(
        server,
        profile,
        row_kind,
    )
    filtered = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        evidence = _row_candidate_evidence(server, profile, candidate)
        track_id = server._recommendation_trim_text(
            (candidate.get("track") or {}).get("id")
        )
        if evidence["negative_feedback_penalty"] >= (0.65 if relaxed else 0.4):
            continue
        if (
            row_kind in {"deep_cuts", "rediscover"}
            and track_id
            and track_id in recent_row_track_ids
        ):
            continue
        if row_kind == "quiet_picks":
            if track_id and track_id in recent_track_ids and len(filtered) >= 4:
                continue
            similarity_supported = (
                evidence["artist_match"]
                or evidence["peer_scene_bonus"] > 0.0
                or evidence["scene_affinity"] >= (0.4 if relaxed else 0.5)
                or evidence["genre_affinity"] >= (0.48 if relaxed else 0.58)
                or evidence["subgenre_affinity"] >= (0.32 if relaxed else 0.42)
            )
            if (
                not evidence["supported_script"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.2 if relaxed else 1.55)
                and not evidence["trusted_source"]
                and not similarity_supported
            ):
                continue
            if (
                "taste_fallback" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.05 if relaxed else 1.3)
                and not evidence["trusted_source"]
                and not similarity_supported
            ):
                continue
            if (
                "exploration_pool" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.1 if relaxed else 1.45)
                and not evidence["trusted_source"]
                and not similarity_supported
            ):
                continue
            if (
                evidence["language_affinity"] <= 0.0
                and not evidence["trusted_source"]
                and not (evidence["artist_match"] or evidence["peer_scene_bonus"] > 0.0)
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.2 if relaxed else 1.55)
            ):
                continue
            if (
                not similarity_supported
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.0 if relaxed else 1.35)
                and not evidence["trusted_source"]
            ):
                continue
        elif row_kind == "rediscover":
            history_supported = any(
                token in source_name
                for source_name in evidence["source_names"]
                for token in (
                    "rediscovery",
                    "history_",
                    "primary_anchor_neighbors",
                    "anchor_neighbors",
                    "offline_library",
                )
            )
            if track_id and track_id in recent_track_ids:
                continue
            if (
                evidence["exploratory_source"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.15 if relaxed else 1.55)
                and not evidence["trusted_source"]
            ):
                continue
            if (
                "taste_fallback" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.2 if relaxed else 1.6)
                and not evidence["trusted_source"]
            ):
                continue
            if (
                not evidence["supported_script"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.25 if relaxed else 1.7)
                and not evidence["trusted_source"]
                and not evidence["artist_match"]
                and evidence["peer_scene_bonus"] <= 0.0
            ):
                continue
            if (
                evidence["language_affinity"] <= 0.0
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.25 if relaxed else 1.65)
                and not history_supported
                and not evidence["trusted_source"]
            ):
                continue
            if (
                not history_supported
                and not evidence["artist_match"]
                and evidence["peer_scene_bonus"] <= 0.0
                and evidence["scene_affinity"] < (0.4 if relaxed else 0.52)
                and evidence["genre_affinity"] < (0.45 if relaxed else 0.58)
            ):
                continue
        elif row_kind == "deep_cuts":
            if (
                not evidence["supported_script"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.4 if relaxed else 1.8)
            ):
                continue
            if (
                evidence["exploratory_source"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.2 if relaxed else 1.55)
                and not evidence["trusted_source"]
            ):
                continue
            if (
                "taste_fallback" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.35 if relaxed else 1.8)
            ):
                continue
            if (
                "exploration_pool" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.25 if relaxed else 1.7)
                and not (evidence["artist_match"] or evidence["peer_scene_bonus"] > 0.0)
            ):
                continue
            if (
                evidence["language_affinity"] <= 0.0
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.45 if relaxed else 1.95)
                and not evidence["trusted_source"]
            ):
                continue
            if track_id and track_id in recent_track_ids and len(filtered) >= 3:
                continue
        elif row_kind == "trending_for_you":
            trend_supported = (
                evidence["artist_match"]
                or evidence["peer_scene_bonus"] > 0.0
                or evidence["scene_affinity"] >= (0.34 if relaxed else 0.46)
                or evidence["genre_affinity"] >= (0.4 if relaxed else 0.52)
                or evidence["subgenre_affinity"] >= (0.26 if relaxed else 0.36)
            )
            if (
                not evidence["supported_script"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.15 if relaxed else 1.55)
                and not evidence["trusted_source"]
                and not trend_supported
            ):
                continue
            if (
                evidence["exploratory_source"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.0 if relaxed else 1.45)
                and not evidence["trusted_source"]
                and not trend_supported
            ):
                continue
            if (
                "exploration_pool" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.1 if relaxed else 1.55)
                and not trend_supported
            ):
                continue
            if (
                "taste_fallback" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (0.95 if relaxed else 1.3)
                and not evidence["trusted_source"]
                and not trend_supported
            ):
                continue
            if (
                evidence["language_affinity"] <= 0.0
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.1 if relaxed else 1.55)
                and not evidence["trusted_source"]
                and not trend_supported
            ):
                continue
        filtered.append(candidate)
    if filtered:
        return filtered
    if row_kind in {"deep_cuts", "trending_for_you", "rediscover"}:
        return []
    return list(candidates or [])


def _quiet_extension_pool_names(row_seed: Dict[str, Any]) -> List[str]:
    pool_names: List[str] = []
    for pool_name in list(row_seed.get("allocator_pool_order") or []):
        normalized = str(pool_name or "").strip()
        if normalized and normalized not in pool_names:
            pool_names.append(normalized)
    for fallback_pool in _prefilter_pool_order("quiet_picks"):
        if fallback_pool not in pool_names:
            pool_names.append(fallback_pool)
    return pool_names


def _row_extension_pool_names(row_kind: str, row_seed: Dict[str, Any]) -> List[str]:
    if row_kind == "quiet_picks":
        return _quiet_extension_pool_names(row_seed)
    pool_names: List[str] = []
    for pool_name in list(row_seed.get("allocator_pool_order") or []):
        normalized = str(pool_name or "").strip()
        if normalized and normalized not in pool_names:
            pool_names.append(normalized)
    fallback_order = {
        "continue_listening": (
            "history_recent",
            "same_artist",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "artist_neighbors",
            "collaborative",
            "history_top",
        ),
        "because_you_played": (
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "artist_neighbors",
            "peer_scene",
            "genre_subgenre",
            "era_neighbors",
            "collaborative",
            "history_recent",
            "history_top",
        ),
        "trending_for_you": (
            "peer_scene",
            "genre_subgenre",
            "era_neighbors",
            "language_safe",
            "popularity_taste",
            "collaborative",
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "history_top",
            "history_recent",
        ),
        "deep_cuts": (
            "peer_scene",
            "genre_subgenre",
            "era_neighbors",
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "rediscovery",
            "collaborative",
            "history_top",
            "history_recent",
            "popularity_taste",
        ),
        "rediscover": (
            "rediscovery",
            "history_recent",
            "history_top",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "artist_neighbors",
            "peer_scene",
            "genre_subgenre",
            "era_neighbors",
            "collaborative",
        ),
    }.get(row_kind, ())
    for fallback_pool in fallback_order:
        if fallback_pool not in pool_names:
            pool_names.append(fallback_pool)
    return pool_names


def _trending_primary_pool_order(
    snapshot: Dict[str, Any],
    allocation_plan: Dict[str, Any] | None = None,
) -> List[str]:
    return _merge_pool_order(
        (
            "peer_scene",
            "genre_subgenre",
            "era_neighbors",
            "language_safe",
            "popularity_taste",
            "collaborative",
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "history_top",
            "history_recent",
            "exploration",
        ),
        list((allocation_plan or {}).get("pool_names") or ()),
        dict(snapshot.get("pools") or {}),
    )
