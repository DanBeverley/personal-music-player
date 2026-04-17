from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import time
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from .feature_store import (
    CATALOG_FEATURE_VERSION,
    SCENE_GRAPH_VERSION,
    TASTE_PROFILE_VERSION,
    attenuate_negative_feedback,
    clear_negative_feedback,
    delete_taste_profile,
    get_artist_feature,
    get_track_feature,
    load_negative_feedback,
    load_taste_profile,
    persistent_store_reads_enabled,
    store_taste_profile,
    upsert_negative_feedback,
    warm_feature_artifacts,
)
from .scene_graph_store import warm_scene_graph_records


_HARD_HIDE_TTL_SECONDS = 45 * 24 * 3600
_DUPLICATE_HIDE_TTL_SECONDS = 30 * 24 * 3600
_SOFT_CLUSTER_TTL_SECONDS = 14 * 24 * 3600
_SKIP_TTL_SECONDS = 3 * 24 * 3600


def _normalized_counter(counter: Counter[str], *, limit: int = 24) -> Dict[str, float]:
    if not counter:
        return {}
    top_items = counter.most_common(limit)
    total = sum(max(float(value), 0.0) for _, value in top_items) or 1.0
    return {
        str(key): round(max(float(value), 0.0) / total, 4)
        for key, value in top_items
        if str(key)
    }


def _top_keys(counter: Counter[str], *, limit: int, threshold: float = 0.0) -> List[str]:
    if not counter:
        return []
    output: List[str] = []
    for key, value in counter.most_common(limit):
        if not str(key):
            continue
        if float(value or 0.0) < float(threshold or 0.0):
            continue
        output.append(str(key))
    return output


def _mean(values: Sequence[float]) -> float:
    numeric = [float(value) for value in values if value is not None]
    if not numeric:
        return 0.0
    return round(sum(numeric) / max(len(numeric), 1), 4)


def _track_groups(profile: Dict[str, Any]) -> List[Tuple[str, List[Dict[str, Any]], float]]:
    return [
        ("session", list(profile.get("last_played_tracks") or [])[:10], 1.85),
        ("session", list(profile.get("anchor_track_snapshots") or [])[:6], 1.55),
        ("session", list(profile.get("recent_track_snapshots") or [])[:12], 1.3),
        ("long_term", list(profile.get("top_track_snapshots") or [])[:14], 1.15),
    ]


def _query_groups(profile: Dict[str, Any]) -> List[Tuple[str, List[str], float]]:
    return [
        ("session", list(profile.get("recent_queries") or [])[:8], 1.05),
        ("long_term", list(profile.get("recent_queries") or [])[:8], 0.82),
        ("long_term", list(profile.get("taste_queries") or [])[:8], 1.18),
    ]


def _query_feature_payload(query: str) -> Dict[str, Any]:
    text = str(query or "").strip()
    return {
        "id": f"query::{text.lower()}",
        "title": text,
        "channel": "",
        "album": "",
    }


def _feedback_signature(feedback_rows: Dict[str, Dict[str, Dict[str, Any]]]) -> str:
    flattened: List[Tuple[str, str, float, int]] = []
    for feedback_type, entries in sorted((feedback_rows or {}).items()):
        for feedback_key, payload in sorted((entries or {}).items()):
            flattened.append(
                (
                    str(feedback_type or ""),
                    str(feedback_key or ""),
                    round(float((payload or {}).get("strength") or 0.0), 4),
                    int(float((payload or {}).get("expires_at") or 0.0)),
                )
            )
    return hashlib.sha1(
        json.dumps(flattened, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _track_signature_payload(track: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(track.get("id") or ""),
        "title": str(track.get("title") or ""),
        "artist": str(track.get("channel") or track.get("artist") or track.get("author") or ""),
        "album": str(track.get("album") or ""),
        "year": str(
            track.get("year")
            or track.get("release_year")
            or track.get("release_date")
            or ""
        ),
    }


def _source_signature(server: Any, profile: Dict[str, Any], feedback_rows: Dict[str, Dict[str, Dict[str, Any]]]) -> str:
    payload = {
        "user_scope_id": str(profile.get("user_scope_id") or "guest"),
        "profile_key": str(profile.get("profile_key") or ""),
        "recent_track_ids": list(profile.get("recent_track_ids") or [])[:16],
        "top_track_ids": list(profile.get("top_track_ids") or [])[:16],
        "recent_queries": list(profile.get("recent_queries") or [])[:8],
        "taste_queries": list(profile.get("taste_queries") or [])[:8],
        "artist_hints": list(profile.get("artist_hints") or [])[:8],
        "album_hints": list(profile.get("album_hints") or [])[:8],
        "novelty_tolerance": float(profile.get("novelty_tolerance") or 0.0),
        "repeat_intensity": float(profile.get("repeat_intensity") or 0.0),
        "feedback_signature": _feedback_signature(feedback_rows),
        "last_played_tracks": [
            _track_signature_payload(track)
            for track in list(profile.get("last_played_tracks") or [])[:8]
            if isinstance(track, dict)
        ],
        "recent_track_snapshots": [
            _track_signature_payload(track)
            for track in list(profile.get("recent_track_snapshots") or [])[:8]
            if isinstance(track, dict)
        ],
        "top_track_snapshots": [
            _track_signature_payload(track)
            for track in list(profile.get("top_track_snapshots") or [])[:8]
            if isinstance(track, dict)
        ],
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _feedback_summary(
    feedback_rows: Dict[str, Dict[str, Dict[str, Any]]],
) -> Dict[str, Any]:
    by_type: Dict[str, Dict[str, float]] = {}
    total = 0
    for feedback_type, entries in (feedback_rows or {}).items():
        current: Dict[str, float] = {}
        for feedback_key, payload in (entries or {}).items():
            strength = float((payload or {}).get("strength") or 0.0)
            if strength <= 0.0:
                continue
            current[str(feedback_key)] = round(strength, 4)
            total += 1
        if current:
            by_type[str(feedback_type)] = current
    return {
        "by_type": by_type,
        "count": total,
    }


def _accumulate_feature_counters(
    *,
    feature: Dict[str, Any],
    weight: float,
    genre_counts: Counter[str],
    subgenre_counts: Counter[str],
    era_counts: Counter[str],
    language_counts: Counter[str],
    script_counts: Counter[str],
    scene_counts: Counter[str],
    artist_counts: Counter[str],
    album_counts: Counter[str],
    title_counts: Counter[str],
    type_counts: Counter[str],
    mood_values: Dict[str, List[float]],
    popularity_values: List[float],
    freshness_values: List[float],
) -> None:
    primary_genre = str(feature.get("primary_genre") or "").strip()
    if primary_genre:
        genre_counts[primary_genre] += weight * 1.0
    for secondary in list(feature.get("secondary_genres") or [])[:4]:
        secondary_value = str(secondary or "").strip()
        if secondary_value and secondary_value != primary_genre:
            genre_counts[secondary_value] += weight * 0.45
    subgenre = str(feature.get("subgenre") or "").strip()
    if subgenre:
        subgenre_counts[subgenre] += weight * 1.0
    era_bucket = str(feature.get("era_bucket") or "").strip()
    if era_bucket:
        era_counts[era_bucket] += weight * 1.0
    language = str(feature.get("language") or "").strip()
    if language:
        language_counts[language] += weight * 1.0
    script = str(feature.get("script") or "").strip()
    if script:
        script_counts[script] += weight * 1.0
    for scene_cluster in list(feature.get("scene_cluster_ids") or [])[:8]:
        cluster_value = str(scene_cluster or "").strip()
        if cluster_value:
            scene_counts[cluster_value] += weight * 1.0
    artist_key = str(feature.get("artist_key") or "").strip()
    if artist_key:
        artist_counts[artist_key] += weight * 1.1
    album_key = str(feature.get("album_key") or "").strip()
    if album_key:
        album_counts[album_key] += weight * 0.9
    title_key = str(feature.get("title_key") or "").strip()
    if title_key:
        title_counts[title_key] += weight * 0.5
    for type_tag in list(feature.get("track_type_tags") or [])[:6]:
        type_value = str(type_tag or "").strip()
        if type_value:
            type_counts[type_value] += weight * 1.0
    for axis_name, axis_value in dict(feature.get("mood_axes") or {}).items():
        if axis_value is None:
            continue
        mood_values.setdefault(str(axis_name), []).append(float(axis_value) * weight)
    popularity_values.append(float(feature.get("popularity") or 0.0) * weight)
    freshness_values.append(float(feature.get("freshness") or 0.0) * weight)


def _derive_taste_profile(server: Any, profile: Dict[str, Any], feedback_rows: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    short_genres: Counter[str] = Counter()
    short_subgenres: Counter[str] = Counter()
    short_eras: Counter[str] = Counter()
    short_languages: Counter[str] = Counter()
    short_scripts: Counter[str] = Counter()
    short_scenes: Counter[str] = Counter()
    short_artists: Counter[str] = Counter()
    short_albums: Counter[str] = Counter()
    short_titles: Counter[str] = Counter()
    short_types: Counter[str] = Counter()
    long_genres: Counter[str] = Counter()
    long_subgenres: Counter[str] = Counter()
    long_eras: Counter[str] = Counter()
    long_languages: Counter[str] = Counter()
    long_scripts: Counter[str] = Counter()
    long_scenes: Counter[str] = Counter()
    long_artists: Counter[str] = Counter()
    long_albums: Counter[str] = Counter()
    long_titles: Counter[str] = Counter()
    long_types: Counter[str] = Counter()
    mood_values: Dict[str, List[float]] = {}
    popularity_values: List[float] = []
    freshness_values: List[float] = []
    album_title_artist_keys: Dict[str, set[str]] = defaultdict(set)
    title_artist_keys: Dict[str, set[str]] = defaultdict(set)

    for group_name, tracks, base_weight in _track_groups(profile):
        for index, track in enumerate(tracks):
            if not isinstance(track, dict):
                continue
            weight = max(base_weight - (index * 0.08), 0.35)
            feature = get_track_feature(server, track)
            if group_name == "session":
                _accumulate_feature_counters(
                    feature=feature,
                    weight=weight,
                    genre_counts=short_genres,
                    subgenre_counts=short_subgenres,
                    era_counts=short_eras,
                    language_counts=short_languages,
                    script_counts=short_scripts,
                    scene_counts=short_scenes,
                    artist_counts=short_artists,
                    album_counts=short_albums,
                    title_counts=short_titles,
                    type_counts=short_types,
                    mood_values=mood_values,
                    popularity_values=popularity_values,
                    freshness_values=freshness_values,
                )
            _accumulate_feature_counters(
                feature=feature,
                weight=max(weight * (0.9 if group_name == "session" else 1.0), 0.25),
                genre_counts=long_genres,
                subgenre_counts=long_subgenres,
                era_counts=long_eras,
                language_counts=long_languages,
                script_counts=long_scripts,
                scene_counts=long_scenes,
                artist_counts=long_artists,
                album_counts=long_albums,
                title_counts=long_titles,
                type_counts=long_types,
                mood_values=mood_values,
                popularity_values=popularity_values,
                freshness_values=freshness_values,
            )
            title_key = str(feature.get("title_key") or "").strip()
            album_key = str(feature.get("album_key") or "").strip()
            artist_key = str(feature.get("artist_key") or "").strip()
            if title_key and artist_key:
                title_artist_keys[title_key].add(artist_key)
            if album_key and artist_key:
                album_title_artist_keys[album_key].add(artist_key)

    for group_name, queries, base_weight in _query_groups(profile):
        for index, query in enumerate(queries):
            text = str(query or "").strip()
            if not text:
                continue
            weight = max(base_weight - (index * 0.08), 0.28)
            feature = get_track_feature(
                server,
                _query_feature_payload(text),
            )
            if group_name == "session":
                _accumulate_feature_counters(
                    feature=feature,
                    weight=weight,
                    genre_counts=short_genres,
                    subgenre_counts=short_subgenres,
                    era_counts=short_eras,
                    language_counts=short_languages,
                    script_counts=short_scripts,
                    scene_counts=short_scenes,
                    artist_counts=short_artists,
                    album_counts=short_albums,
                    title_counts=short_titles,
                    type_counts=short_types,
                    mood_values=mood_values,
                    popularity_values=popularity_values,
                    freshness_values=freshness_values,
                )
            _accumulate_feature_counters(
                feature=feature,
                weight=max(weight * (0.92 if group_name == "session" else 1.0), 0.22),
                genre_counts=long_genres,
                subgenre_counts=long_subgenres,
                era_counts=long_eras,
                language_counts=long_languages,
                script_counts=long_scripts,
                scene_counts=long_scenes,
                artist_counts=long_artists,
                album_counts=long_albums,
                title_counts=long_titles,
                type_counts=long_types,
                mood_values=mood_values,
                popularity_values=popularity_values,
                freshness_values=freshness_values,
            )

    peer_artist_counter: Counter[str] = Counter()
    for index, artist_name in enumerate(
        server._recommendation_unique_strings(
            [
                *(profile.get("top_artists") or []),
                *(profile.get("artist_hints") or []),
                *(profile.get("listened_artists") or []),
            ],
            12,
        )
    ):
        artist_feature = get_artist_feature(server, {"name": artist_name})
        weight = max(1.5 - (index * 0.12), 0.35)
        artist_key = str(artist_feature.get("artist_key") or "").strip()
        if artist_key:
            long_artists[artist_key] += weight * 0.7
        for peer_artist in list(artist_feature.get("peer_artist_ids") or [])[:8]:
            peer_key = str(peer_artist or "").strip()
            if peer_key:
                peer_artist_counter[peer_key] += weight
        for scene_cluster in list(artist_feature.get("scene_cluster_ids") or [])[:6]:
            cluster_value = str(scene_cluster or "").strip()
            if cluster_value:
                long_scenes[cluster_value] += weight * 0.45

    collaborative_artist_scores = dict(
        ((profile.get("collaborative") or {}).get("artist_scores") or {})
    )
    for index, (artist_key, score) in enumerate(
        sorted(
            collaborative_artist_scores.items(),
            key=lambda item: float(item[1] or 0.0),
            reverse=True,
        )[:24]
    ):
        normalized_key = server._normalize_text(artist_key)
        if not normalized_key:
            continue
        weight = max(float(score or 0.0), 0.0) * max(1.0 - (index * 0.03), 0.2)
        long_artists[normalized_key] += weight
        peer_artist_counter[normalized_key] += weight * 0.6

    dominant_script = _top_keys(long_scripts, limit=1)
    dominant_language = _top_keys(long_languages, limit=1)
    dominant_era = _top_keys(long_eras, limit=1)
    album_depth_preference = min(
        len([key for key, count in long_albums.items() if count >= 0.9]) / 6.0,
        1.0,
    )
    popularity_tolerance = max(
        0.08,
        min(
            0.95,
            _mean(popularity_values) or (0.42 + (float(profile.get("repeat_intensity") or 0.0) * 0.18)),
        ),
    )
    novelty_tolerance = max(
        0.05,
        min(
            0.95,
            float(profile.get("novelty_tolerance") or 0.0)
            + ((_mean(freshness_values) - 0.5) * 0.18),
        ),
    )

    taste_profile = {
        "user_scope_id": str(profile.get("user_scope_id") or "guest"),
        "profile_version": TASTE_PROFILE_VERSION,
        "catalog_feature_version": CATALOG_FEATURE_VERSION,
        "scene_graph_version": SCENE_GRAPH_VERSION,
        "feature_source": "stored_enriched",
        "long_term": {
            "genres": _normalized_counter(long_genres),
            "subgenres": _normalized_counter(long_subgenres),
            "eras": _normalized_counter(long_eras),
            "languages": _normalized_counter(long_languages),
            "scripts": _normalized_counter(long_scripts),
            "scenes": _normalized_counter(long_scenes),
            "artists": _normalized_counter(long_artists),
            "albums": _normalized_counter(long_albums),
            "types": _normalized_counter(long_types),
        },
        "session": {
            "genres": _normalized_counter(short_genres),
            "subgenres": _normalized_counter(short_subgenres),
            "eras": _normalized_counter(short_eras),
            "languages": _normalized_counter(short_languages),
            "scripts": _normalized_counter(short_scripts),
            "scenes": _normalized_counter(short_scenes),
            "artists": _normalized_counter(short_artists),
            "albums": _normalized_counter(short_albums),
            "types": _normalized_counter(short_types),
        },
        "preferred_genres": _top_keys(long_genres, limit=6, threshold=0.45),
        "preferred_subgenres": _top_keys(long_subgenres, limit=6, threshold=0.35),
        "dominant_script": dominant_script[0] if dominant_script else "latin",
        "supported_scripts": _top_keys(long_scripts, limit=5, threshold=0.45),
        "dominant_language": dominant_language[0] if dominant_language else "english",
        "supported_languages": _top_keys(long_languages, limit=5, threshold=0.45),
        "dominant_era": dominant_era[0] if dominant_era else "",
        "supported_eras": _top_keys(long_eras, limit=6, threshold=0.45),
        "supported_type_tags": _top_keys(long_types, limit=6, threshold=0.3),
        "dominant_artist_keys": _top_keys(long_artists, limit=4, threshold=0.5),
        "scene_artist_scores": {
            key: round(float(value), 4)
            for key, value in long_artists.most_common(24)
            if key
        },
        "scene_cluster_scores": {
            key: round(float(value), 4)
            for key, value in long_scenes.most_common(24)
            if key
        },
        "peer_scene_keys": _top_keys(peer_artist_counter, limit=14, threshold=0.25),
        "artist_neighborhood_preferences": _top_keys(peer_artist_counter, limit=18, threshold=0.18),
        "affinity_artists": _top_keys(long_artists, limit=16, threshold=0.4),
        "affinity_albums": _top_keys(long_albums, limit=16, threshold=0.3),
        "affinity_titles": _top_keys(long_titles, limit=24, threshold=0.2),
        "album_title_artist_keys": {
            title_key: sorted(artist_keys)
            for title_key, artist_keys in album_title_artist_keys.items()
            if title_key and artist_keys
        },
        "title_artist_keys": {
            title_key: sorted(artist_keys)
            for title_key, artist_keys in title_artist_keys.items()
            if title_key and artist_keys
        },
        "mood_profile": {
            axis_name: _mean(values)
            for axis_name, values in mood_values.items()
            if values
        },
        "album_depth_preference": round(album_depth_preference, 4),
        "novelty_tolerance": round(novelty_tolerance, 4),
        "popularity_tolerance": round(popularity_tolerance, 4),
        "negative_feedback": _feedback_summary(feedback_rows),
    }
    return taste_profile


def build_taste_profile(
    server: Any,
    profile: Dict[str, Any],
    *,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    cached = profile.get("taste_profile")
    if isinstance(cached, dict) and cached and not force_refresh:
        return cached
    user_scope_id = str(profile.get("user_scope_id") or "guest")
    feedback_rows = load_negative_feedback(server, user_scope_id=user_scope_id)
    source_signature = _source_signature(server, profile, feedback_rows)
    if not force_refresh:
        stored = load_taste_profile(server, user_scope_id=user_scope_id)
        if (
            isinstance(stored, dict)
            and stored
            and str(stored.get("source_signature") or "") == source_signature
        ):
            stored_payload = dict(stored)
            stored_payload.setdefault("feature_source", "stored_enriched")
            profile["taste_profile"] = stored_payload
            return stored_payload
    taste_profile = _derive_taste_profile(server, profile, feedback_rows)
    if not persistent_store_reads_enabled():
        transient_payload = dict(taste_profile)
        transient_payload["source_signature"] = source_signature
        transient_payload.setdefault("feature_source", "derived_request")
        profile["taste_profile"] = transient_payload
        return transient_payload
    stored = store_taste_profile(
        server,
        user_scope_id=user_scope_id,
        payload=taste_profile,
        source_signature=source_signature,
    )
    stored["source_signature"] = source_signature
    profile["taste_profile"] = stored
    return stored


def warm_profile_feature_artifacts(
    server: Any,
    profile: Dict[str, Any],
    *,
    extra_tracks: Sequence[Dict[str, Any]] | None = None,
    extra_artists: Sequence[Dict[str, Any] | str] | None = None,
    extra_albums: Sequence[Dict[str, Any]] | None = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
            *(profile.get("top_track_snapshots") or []),
            *(extra_tracks or []),
        ],
        72,
    )
    artists: List[Dict[str, Any] | str] = [
        *(extra_artists or []),
        *list(profile.get("top_artists") or [])[:12],
        *list(profile.get("artist_hints") or [])[:12],
        *list(profile.get("listened_artists") or [])[:12],
    ]
    for track in tracks:
        if not isinstance(track, dict):
            continue
        artist_name = server._recommendation_trim_text(
            track.get("channel") or track.get("artist") or track.get("author")
        )
        if artist_name:
            artists.append(artist_name)
    counts = warm_feature_artifacts(
        server,
        tracks=tracks,
        artists=artists,
        albums=extra_albums or [],
    )
    artist_features = [
        get_artist_feature(server, artist)
        for artist in artists
        if artist is not None
    ]
    scene_graph_counts = warm_scene_graph_records(
        server,
        artist_features=artist_features,
    )
    taste_profile = build_taste_profile(
        server,
        profile,
        force_refresh=force_refresh,
    )
    return {
        "counts": counts,
        "scene_graph_counts": scene_graph_counts,
        "taste_profile_version": taste_profile.get("profile_version") or TASTE_PROFILE_VERSION,
        "catalog_feature_version": taste_profile.get("catalog_feature_version") or CATALOG_FEATURE_VERSION,
        "scene_graph_version": taste_profile.get("scene_graph_version") or SCENE_GRAPH_VERSION,
        "feature_source": taste_profile.get("feature_source") or "stored_enriched",
    }


def _track_payload_from_event(server: Any, req) -> Dict[str, Any]:
    metadata = dict(getattr(req, "metadata", {}) or {})
    artist_name = server._recommendation_trim_text(
        getattr(req, "artist_name", None)
        or metadata.get("channel")
        or metadata.get("artist")
        or metadata.get("author")
    )
    payload = {
        "id": server._recommendation_trim_text(getattr(req, "track_id", "") or metadata.get("track_id")),
        "title": metadata.get("title") or metadata.get("name") or "",
        "channel": artist_name or "",
        "artist": artist_name or "",
        "author": artist_name or "",
        "album": metadata.get("album") or metadata.get("album_title") or "",
        "year": metadata.get("year") or metadata.get("release_year") or "",
        "release_date": metadata.get("release_date") or metadata.get("published") or "",
    }
    return payload


def _duplicate_feedback_key(feature: Dict[str, Any]) -> str:
    title_key = str(feature.get("title_key") or "").strip()
    artist_key = str(feature.get("artist_key") or "").strip()
    if not title_key or not artist_key:
        return ""
    return f"{title_key}|{artist_key}"


def _scene_feedback_keys(feature: Dict[str, Any]) -> List[Tuple[str, str, float]]:
    entries: List[Tuple[str, str, float]] = []
    artist_key = str(feature.get("artist_key") or "").strip()
    if artist_key:
        entries.append(("artist_cluster", artist_key, 0.55))
    primary_genre = str(feature.get("primary_genre") or "").strip()
    if primary_genre:
        entries.append(("genre_cluster", primary_genre, 0.38))
    subgenre = str(feature.get("subgenre") or "").strip()
    if subgenre:
        entries.append(("subgenre_cluster", subgenre, 0.3))
    language = str(feature.get("language") or "").strip()
    if language and language != "unknown":
        entries.append(("language_cluster", language, 0.3))
    script = str(feature.get("script") or "").strip()
    if script and script != "unknown":
        entries.append(("script_cluster", script, 0.22))
    for scene_cluster in list(feature.get("scene_cluster_ids") or [])[:6]:
        cluster_value = str(scene_cluster or "").strip()
        if cluster_value:
            entries.append(("scene_cluster", cluster_value, 0.45))
    return entries


def _feedback_action(req) -> str:
    event_type = str(getattr(req, "event_type", "") or "").strip().lower()
    metadata = dict(getattr(req, "metadata", {}) or {})
    reason = str(metadata.get("reason") or "").strip().lower()
    if reason == "history_delete":
        return "history_delete"
    if event_type == "skip":
        return "negative_skip"
    if event_type in {"library", "download", "save"}:
        return "positive_save"
    if event_type in {"play", "complete", "replay"}:
        return "positive_replay"
    return event_type or "play"


def apply_interaction_feedback(server: Any, req) -> Dict[str, Any]:
    user_scope_id = server._assistant_safe_scope_id(getattr(req, "user_scope_id", "guest") or "guest")
    track_payload = _track_payload_from_event(server, req)
    track_id = str(track_payload.get("id") or "").strip()
    if not track_id:
        return {"applied": False, "reason": "missing_track_id"}
    feature = get_track_feature(server, track_payload)
    duplicate_key = _duplicate_feedback_key(feature)
    action = _feedback_action(req)
    applied_keys: List[str] = []
    negative_feedback_applied = False

    if action == "history_delete":
        upsert_negative_feedback(
            server,
            user_scope_id=user_scope_id,
            feedback_type="exact_track",
            feedback_key=track_id,
            strength=1.0,
            ttl_seconds=_HARD_HIDE_TTL_SECONDS,
            metadata={"event": action},
        )
        applied_keys.append(f"exact_track:{track_id}")
        if duplicate_key:
            upsert_negative_feedback(
                server,
                user_scope_id=user_scope_id,
                feedback_type="duplicate_track",
                feedback_key=duplicate_key,
                strength=0.95,
                ttl_seconds=_DUPLICATE_HIDE_TTL_SECONDS,
                metadata={"event": action},
            )
            applied_keys.append(f"duplicate_track:{duplicate_key}")
        for feedback_type, feedback_key, strength in _scene_feedback_keys(feature):
            upsert_negative_feedback(
                server,
                user_scope_id=user_scope_id,
                feedback_type=feedback_type,
                feedback_key=feedback_key,
                strength=strength,
                ttl_seconds=_SOFT_CLUSTER_TTL_SECONDS,
                metadata={"event": action},
            )
            applied_keys.append(f"{feedback_type}:{feedback_key}")
        negative_feedback_applied = True
    elif action == "negative_skip":
        upsert_negative_feedback(
            server,
            user_scope_id=user_scope_id,
            feedback_type="exact_track",
            feedback_key=track_id,
            strength=0.42,
            ttl_seconds=_SKIP_TTL_SECONDS,
            metadata={"event": action},
        )
        applied_keys.append(f"exact_track:{track_id}")
        if duplicate_key:
            upsert_negative_feedback(
                server,
                user_scope_id=user_scope_id,
                feedback_type="duplicate_track",
                feedback_key=duplicate_key,
                strength=0.28,
                ttl_seconds=_SKIP_TTL_SECONDS,
                metadata={"event": action},
            )
            applied_keys.append(f"duplicate_track:{duplicate_key}")
        for feedback_type, feedback_key, strength in _scene_feedback_keys(feature):
            upsert_negative_feedback(
                server,
                user_scope_id=user_scope_id,
                feedback_type=feedback_type,
                feedback_key=feedback_key,
                strength=max(strength * 0.35, 0.12),
                ttl_seconds=_SKIP_TTL_SECONDS,
                metadata={"event": action},
            )
            applied_keys.append(f"{feedback_type}:{feedback_key}")
        negative_feedback_applied = True
    elif action in {"positive_save", "positive_replay"}:
        clear_negative_feedback(
            server,
            user_scope_id=user_scope_id,
            feedback_type="exact_track",
            feedback_key=track_id,
        )
        if duplicate_key:
            if action == "positive_save":
                clear_negative_feedback(
                    server,
                    user_scope_id=user_scope_id,
                    feedback_type="duplicate_track",
                    feedback_key=duplicate_key,
                )
            else:
                attenuate_negative_feedback(
                    server,
                    user_scope_id=user_scope_id,
                    feedback_type="duplicate_track",
                    feedback_key=duplicate_key,
                    factor=0.45,
                )
        for feedback_type, feedback_key, _strength in _scene_feedback_keys(feature):
            attenuate_negative_feedback(
                server,
                user_scope_id=user_scope_id,
                feedback_type=feedback_type,
                feedback_key=feedback_key,
                factor=0.55 if action == "positive_save" else 0.72,
            )
        applied_keys.append(f"positive:{track_id}")

    delete_taste_profile(server, user_scope_id=user_scope_id)
    return {
        "applied": True,
        "action": action,
        "track_id": track_id,
        "negative_feedback_applied": negative_feedback_applied,
        "applied_keys": applied_keys[:24],
    }
