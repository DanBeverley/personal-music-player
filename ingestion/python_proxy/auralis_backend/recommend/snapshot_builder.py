from __future__ import annotations

from typing import Any, Dict, List
import time

from .allocator import (
    build_profile_allocator_features,
    summarize_snapshot_pool_features,
)
from .candidate_snapshot_stage import (
    assemble_snapshot_payload,
    prepare_snapshot_inputs,
    resolve_snapshot_fetches,
)
from .home_config import (
    _HOME_ALBUM_CAP,
    _HOME_ANCHOR_LIMIT,
    _HOME_ARTIST_CAP,
    _HOME_COLLAB_POOL_CAP,
    _HOME_FALLBACK_POOL_CAP,
    _HOME_HISTORY_POOL_CAP,
    _HOME_POOL_CANDIDATE_CAP,
)
from .pool_runtime import (
    _build_feature_aware_pools,
    _combine_pools,
    _post_filter_row_candidates,
    _prefilter_pool_order,
    _prefiltered_pool_name,
    _track_list_to_candidates,
    _trim_candidate_pool,
)
from .snapshot_support_runtime import (
    build_album_items,
    build_artist_artifacts,
    build_profile_artist_items,
    fetch_anchor_candidate_pools,
    timed_call,
)
from .source_runtime import _recommendation_home_fallback_tracks


def build_home_candidate_snapshot(
    *,
    server: Any,
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    preparation = prepare_snapshot_inputs(
        server=server,
        profile=profile,
        started_at=started_at,
        anchor_limit=_HOME_ANCHOR_LIMIT,
        history_pool_cap=_HOME_HISTORY_POOL_CAP,
    )
    fetched = resolve_snapshot_fetches(
        server=server,
        profile=profile,
        preparation=preparation,
        collab_pool_cap=_HOME_COLLAB_POOL_CAP,
        fallback_pool_cap=_HOME_FALLBACK_POOL_CAP,
        history_pool_cap=_HOME_HISTORY_POOL_CAP,
        timed_call_fn=timed_call,
        home_fallback_tracks_fn=_recommendation_home_fallback_tracks,
        build_artist_artifacts_fn=build_artist_artifacts,
        fetch_anchor_candidate_pools_fn=fetch_anchor_candidate_pools,
        build_album_items_fn=build_album_items,
    )
    return assemble_snapshot_payload(
        server=server,
        profile=profile,
        started_at=started_at,
        preparation=preparation,
        fetched=fetched,
        history_pool_cap=_HOME_HISTORY_POOL_CAP,
        home_fallback_pool_cap=_HOME_FALLBACK_POOL_CAP,
        home_pool_candidate_cap=_HOME_POOL_CANDIDATE_CAP,
        track_list_to_candidates_fn=_track_list_to_candidates,
        trim_candidate_pool_fn=_trim_candidate_pool,
        build_feature_aware_pools_fn=lambda runtime_server, runtime_profile, base_pools: _build_feature_aware_pools(
            runtime_server,
            runtime_profile,
            base_pools,
            pool_candidate_cap=_HOME_POOL_CANDIDATE_CAP,
        ),
        combine_pools_fn=_combine_pools,
        prefilter_pool_order_fn=_prefilter_pool_order,
        post_filter_row_candidates_fn=_post_filter_row_candidates,
        prefiltered_pool_name_fn=_prefiltered_pool_name,
        build_profile_allocator_features_fn=build_profile_allocator_features,
        summarize_snapshot_pool_features_fn=summarize_snapshot_pool_features,
    )


def build_home_candidate_snapshot_fallback(
    *,
    server: Any,
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    stage_timings: Dict[str, int] = {}
    recent_track_ids = set(profile.get("recent_track_ids") or [])
    recent_history_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
        ],
        _HOME_HISTORY_POOL_CAP,
    )
    top_history_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("top_track_snapshots") or []),
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
        ],
        _HOME_HISTORY_POOL_CAP,
    )
    collaborative_ids = list(
        ((profile.get("collaborative") or {}).get("candidate_track_ids") or [])
    )[:_HOME_COLLAB_POOL_CAP]
    outer_executor = getattr(
        server,
        "recommendation_row_executor",
        None,
    ) or getattr(server, "recommendation_executor")
    fetch_futures = {
        "collaborative": outer_executor.submit(
            timed_call,
            server._recommendation_fetch_tracks_for_ids,
            collaborative_ids,
            _HOME_COLLAB_POOL_CAP,
        ),
        "fallback": outer_executor.submit(
            timed_call,
            _recommendation_home_fallback_tracks,
            profile,
            limit=_HOME_FALLBACK_POOL_CAP,
        ),
        "offline_tracks": outer_executor.submit(
            timed_call,
            server._recommendation_fetch_tracks_for_ids,
            list(profile.get("offline_track_ids") or profile.get("library_track_ids") or []),
            _HOME_HISTORY_POOL_CAP,
        ),
        "albums": outer_executor.submit(
            timed_call,
            build_album_items,
            server,
            profile,
        ),
        "artists": outer_executor.submit(
            timed_call,
            build_artist_artifacts,
            server,
            profile,
        ),
    }

    collaborative_tracks = []
    fallback_tracks = []
    offline_tracks = []
    album_items = []
    artist_items = []
    for future_name, future in fetch_futures.items():
        try:
            result, elapsed_ms = future.result()
        except Exception:
            result, elapsed_ms = [], 0
        stage_key = {
            "collaborative": "fallback_collaborative_fetch_ms",
            "fallback": "fallback_pool_fetch_ms",
            "offline_tracks": "fallback_offline_fetch_ms",
            "albums": "fallback_album_pool_ms",
            "artists": "fallback_artist_pool_ms",
        }.get(future_name)
        if stage_key:
            stage_timings[stage_key] = int(elapsed_ms or 0)
        if future_name == "collaborative":
            collaborative_tracks = list(result or [])
        elif future_name == "fallback":
            fallback_tracks = list(result or [])
        elif future_name == "offline_tracks":
            offline_tracks = list(result or [])
        elif future_name == "albums":
            album_items = list(result or [])
        elif future_name == "artists":
            artifact_payload = dict(result or {})
            artist_items = list(
                artifact_payload.get("artists")
                or build_profile_artist_items(server, profile)
            )

    rediscovery_tracks = [
        track
        for track in top_history_tracks
        if server._recommendation_trim_text(track.get("id")) not in recent_track_ids
    ][:_HOME_HISTORY_POOL_CAP]
    exploration_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(collaborative_tracks[_HOME_COLLAB_POOL_CAP // 2:] or []),
            *(fallback_tracks[_HOME_FALLBACK_POOL_CAP // 3:] or []),
        ],
        _HOME_HISTORY_POOL_CAP,
    )

    pools: Dict[str, List[Dict[str, Any]]] = {
        "history_recent": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                recent_history_tracks,
                generator_name="history_recent",
                base_score=5.0,
                reason="From your recent listening history.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "history_top": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                top_history_tracks,
                generator_name="history_top",
                base_score=5.1,
                reason="You come back to these often.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "offline_library": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                offline_tracks,
                generator_name="offline_library",
                base_score=4.6,
                reason="Ready when you need it offline.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "collaborative": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                collaborative_tracks,
                generator_name="collaborative_home",
                base_score=4.4,
                reason="Listeners with similar taste also played this.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "artist_neighbors": [],
        "anchor_neighbors": [],
        "primary_anchor_neighbors": [],
        "taste_fallback": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                fallback_tracks,
                generator_name="taste_fallback",
                base_score=2.6,
                reason="Taste-filtered fallback while the feed warms up.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "rediscovery": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                rediscovery_tracks,
                generator_name="rediscovery_history",
                base_score=4.0,
                reason="A favorite worth bringing back.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "exploration": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                exploration_tracks,
                generator_name="exploration_pool",
                base_score=2.8,
                reason="A measured step outside your usual rotation.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
    }
    feature_pool_started_at = time.perf_counter()
    pools.update(
        _build_feature_aware_pools(
            server,
            profile,
            pools,
        )
    )
    stage_timings["feature_pool_ms"] = int(
        (time.perf_counter() - feature_pool_started_at) * 1000
    )
    for row_kind in ("quiet_picks", "deep_cuts"):
        prefiltered_candidates, _ = _combine_pools(
            server,
            {"pools": pools},
            _prefilter_pool_order(row_kind),
            limit=max(_HOME_POOL_CANDIDATE_CAP * 2, 72),
        )
        prefiltered_candidates = _post_filter_row_candidates(
            server,
            row_kind,
            profile,
            prefiltered_candidates,
        )
        if prefiltered_candidates:
            pools[_prefiltered_pool_name(row_kind)] = _trim_candidate_pool(
                server,
                prefiltered_candidates,
                limit=max(_HOME_POOL_CANDIDATE_CAP, 56),
            )
    return {
        "generated_at": time.time(),
        "build_ms": int((time.perf_counter() - started_at) * 1000),
        "pool_counts": {key: len(value or []) for key, value in pools.items()},
        "row_mode": "candidate_snapshot_fallback_v43",
        "albums_count": len(album_items),
        "artists_count": len(artist_items),
        "primary_anchor_id": "",
        "quiet_seed": server._recommendation_quiet_base_query(profile),
        "profile_allocator_features": build_profile_allocator_features(profile),
        "pool_allocator_features": summarize_snapshot_pool_features(
            server,
            profile=profile,
            pools=pools,
        ),
        "stage_timings_ms": {
            **stage_timings,
            "fallback_snapshot_ms": int((time.perf_counter() - started_at) * 1000),
        },
        "pools": pools,
        "albums": album_items,
        "artists": artist_items,
    }


def build_home_candidate_snapshot_fast_fallback(
    *,
    server: Any,
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    recent_track_ids = set(profile.get("recent_track_ids") or [])
    recent_history_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
        ],
        max(12, _HOME_HISTORY_POOL_CAP // 2),
    )
    top_history_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("top_track_snapshots") or []),
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
        ],
        max(12, _HOME_HISTORY_POOL_CAP // 2),
    )
    rediscovery_tracks = [
        track
        for track in top_history_tracks
        if server._recommendation_trim_text(track.get("id")) not in recent_track_ids
    ][: max(8, _HOME_HISTORY_POOL_CAP // 2)]
    pools: Dict[str, List[Dict[str, Any]]] = {
        "history_recent": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                recent_history_tracks,
                generator_name="history_recent",
                base_score=5.0,
                reason="From your recent listening history.",
            ),
            limit=max(16, _HOME_POOL_CANDIDATE_CAP // 2),
        ),
        "history_top": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                top_history_tracks,
                generator_name="history_top",
                base_score=4.8,
                reason="You come back to these often.",
            ),
            limit=max(16, _HOME_POOL_CANDIDATE_CAP // 2),
        ),
        "rediscovery": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                rediscovery_tracks,
                generator_name="rediscovery_history",
                base_score=3.9,
                reason="A favorite worth bringing back.",
            ),
            limit=max(12, _HOME_POOL_CANDIDATE_CAP // 3),
        ),
        "offline_library": [],
        "collaborative": [],
        "artist_neighbors": [],
        "anchor_neighbors": [],
        "primary_anchor_neighbors": [],
        "taste_fallback": [],
        "exploration": [],
    }
    return {
        "generated_at": time.time(),
        "build_ms": int((time.perf_counter() - started_at) * 1000),
        "pool_counts": {key: len(value or []) for key, value in pools.items()},
        "row_mode": "candidate_snapshot_launch_fast_v1",
        "albums_count": 0,
        "artists_count": 0,
        "primary_anchor_id": "",
        "quiet_seed": "",
        "profile_allocator_features": build_profile_allocator_features(profile),
        "pool_allocator_features": summarize_snapshot_pool_features(
            server,
            profile=profile,
            pools=pools,
        ),
        "stage_timings_ms": {
            "launch_fast_snapshot_ms": int((time.perf_counter() - started_at) * 1000),
        },
        "pools": pools,
        "albums": [],
        "artists": [],
    }


def trim_home_candidate_snapshot(
    server: Any,
    snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    pools = {}
    for pool_name, candidates in dict(snapshot.get("pools") or {}).items():
        pools[pool_name] = _trim_candidate_pool(
            server,
            list(candidates or []),
            limit=_HOME_POOL_CANDIDATE_CAP,
        )
    albums = []
    for album in list(snapshot.get("albums") or [])[:_HOME_ALBUM_CAP]:
        if isinstance(album, dict):
            albums.append(dict(album))
    artists = []
    for artist in list(snapshot.get("artists") or [])[:_HOME_ARTIST_CAP]:
        if isinstance(artist, dict):
            artists.append(dict(artist))
    return {
        "generated_at": float(snapshot.get("generated_at") or 0.0),
        "build_ms": int(snapshot.get("build_ms") or 0),
        "pool_counts": {key: len(value or []) for key, value in pools.items()},
        "row_mode": snapshot.get("row_mode") or "candidate_snapshot_v42",
        "albums_count": len(albums),
        "artists_count": len(artists),
        "primary_anchor_id": snapshot.get("primary_anchor_id") or "",
        "quiet_seed": snapshot.get("quiet_seed") or "",
        "profile_allocator_features": dict(
            snapshot.get("profile_allocator_features") or {}
        ),
        "pool_allocator_features": {
            pool_name: dict(features or {})
            for pool_name, features in dict(
                snapshot.get("pool_allocator_features") or {}
            ).items()
        },
        "stage_timings_ms": dict(snapshot.get("stage_timings_ms") or {}),
        "pools": pools,
        "albums": albums,
        "artists": artists,
        "artist_artifact_meta": dict(snapshot.get("artist_artifact_meta") or {}),
    }


def snapshot_substrate_mode(snapshot: Dict[str, Any] | None) -> str:
    payload = dict(snapshot or {})
    resolved_from = str(payload.get("resolved_from") or "").strip().lower()
    pool_counts = {
        str(pool_name): int(count or 0)
        for pool_name, count in dict(payload.get("pool_counts") or {}).items()
    }
    if "fallback" in resolved_from or "error" in resolved_from:
        return "thin_core"
    history_signal = sum(
        1
        for pool_name in ("history_recent", "history_top", "collaborative")
        if int(pool_counts.get(pool_name) or 0) > 0
    )
    anchor_signal = sum(
        1
        for pool_name in (
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
        )
        if int(pool_counts.get(pool_name) or 0) > 0
    )
    feature_signal = sum(
        1
        for pool_name in ("peer_scene", "genre_subgenre", "popularity_taste")
        if int(pool_counts.get(pool_name) or 0) > 0
    )
    richness_signal = sum(
        1
        for pool_name in (
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "peer_scene",
            "genre_subgenre",
            "popularity_taste",
        )
        if int(pool_counts.get(pool_name) or 0) > 0
    )
    if history_signal <= 0:
        return "thin_core"
    if anchor_signal >= 2:
        return "rich_personalized"
    if anchor_signal >= 1 and (
        feature_signal >= 1
        or int(pool_counts.get("collaborative") or 0) > 0
        or int(pool_counts.get("history_top") or 0) > 0
    ):
        return "rich_personalized"
    if feature_signal >= 2 and int(pool_counts.get("collaborative") or 0) > 0:
        return "rich_personalized"
    if richness_signal < 2:
        return "thin_core"
    return "rich_personalized"
