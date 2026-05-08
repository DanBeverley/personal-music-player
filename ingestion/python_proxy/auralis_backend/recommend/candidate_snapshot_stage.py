from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Sequence, Tuple


@dataclass
class SnapshotPreparation:
    recent_track_ids: set[str]
    anchor_tracks: List[Dict[str, Any]]
    primary_anchor_track: Dict[str, Any] | None
    recent_history_tracks: List[Dict[str, Any]]
    top_history_tracks: List[Dict[str, Any]]
    stage_timings: Dict[str, int]


@dataclass
class SnapshotFetchResults:
    collaborative_tracks: List[Dict[str, Any]]
    fallback_tracks: List[Dict[str, Any]]
    artist_neighbor_candidates: List[Dict[str, Any]]
    anchor_neighbor_candidates: List[Dict[str, Any]]
    primary_anchor_candidates: List[Dict[str, Any]]
    rediscovery_neighbor_candidates: List[Dict[str, Any]]
    rediscovery_tracks: List[Dict[str, Any]]
    offline_tracks: List[Dict[str, Any]]
    album_items: List[Dict[str, Any]]
    artist_items: List[Dict[str, Any]]
    stage_timings: Dict[str, int]
    artist_artifact_meta: Dict[str, Any] = field(default_factory=dict)


def prepare_snapshot_inputs(
    *,
    server: Any,
    profile: Dict[str, Any],
    started_at: float,
    anchor_limit: int,
    history_pool_cap: int,
) -> SnapshotPreparation:
    recent_track_ids = set(profile.get("recent_track_ids") or [])
    anchor_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
            *(profile.get("top_track_snapshots") or []),
        ],
        anchor_limit,
    )
    primary_anchor_track = anchor_tracks[0] if anchor_tracks else None

    recent_history_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
        ],
        history_pool_cap,
    )
    top_history_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("top_track_snapshots") or []),
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
        ],
        history_pool_cap,
    )
    if len(top_history_tracks) < history_pool_cap:
        seen_top_ids = {
            server._recommendation_trim_text(track.get("id"))
            for track in top_history_tracks
            if isinstance(track, dict)
        }
        missing_top_ids = [
            track_id
            for track_id in list(profile.get("top_track_ids") or [])
            if track_id not in seen_top_ids
        ]
        if missing_top_ids:
            top_history_tracks.extend(
                server._recommendation_fetch_tracks_for_ids(
                    missing_top_ids,
                    limit=max(history_pool_cap - len(top_history_tracks), 0),
                )
            )
            top_history_tracks = server._recommendation_unique_snapshot_tracks(
                top_history_tracks,
                history_pool_cap,
            )
    return SnapshotPreparation(
        recent_track_ids=recent_track_ids,
        anchor_tracks=anchor_tracks,
        primary_anchor_track=primary_anchor_track,
        recent_history_tracks=recent_history_tracks,
        top_history_tracks=top_history_tracks,
        stage_timings={
            "history_prepare_ms": int((time.perf_counter() - started_at) * 1000),
        },
    )


def resolve_snapshot_fetches(
    *,
    server: Any,
    profile: Dict[str, Any],
    preparation: SnapshotPreparation,
    collab_pool_cap: int,
    fallback_pool_cap: int,
    history_pool_cap: int,
    timed_call_fn: Callable[..., Tuple[Any, int]],
    home_fallback_tracks_fn: Callable[..., List[Dict[str, Any]]],
    build_artist_artifacts_fn: Callable[..., Dict[str, Any]],
    fetch_anchor_candidate_pools_fn: Callable[..., Dict[str, Any]],
    build_album_items_fn: Callable[..., List[Dict[str, Any]]],
) -> SnapshotFetchResults:
    collaborative_ids = list(
        ((profile.get("collaborative") or {}).get("candidate_track_ids") or [])
    )
    rediscovery_ids = [
        track_id
        for track_id in list(profile.get("top_track_ids") or [])
        if track_id not in preparation.recent_track_ids
    ]
    outer_executor = getattr(server, "recommendation_row_executor", None) or getattr(server, "recommendation_executor")
    fetch_futures = {
        "collaborative": outer_executor.submit(
            timed_call_fn,
            server._recommendation_fetch_tracks_for_ids,
            collaborative_ids,
            collab_pool_cap,
        ),
        "fallback": outer_executor.submit(
            timed_call_fn,
            home_fallback_tracks_fn,
            profile,
            limit=fallback_pool_cap,
        ),
        "artist_artifacts": outer_executor.submit(
            timed_call_fn,
            build_artist_artifacts_fn,
            server,
            profile,
        ),
        "anchor_neighbors": outer_executor.submit(
            fetch_anchor_candidate_pools_fn,
            server,
            preparation.anchor_tracks,
            preparation.recent_track_ids,
        ),
        "rediscovery_tracks": outer_executor.submit(
            timed_call_fn,
            server._recommendation_fetch_tracks_for_ids,
            rediscovery_ids,
            history_pool_cap,
        ),
        "offline_tracks": outer_executor.submit(
            timed_call_fn,
            server._recommendation_fetch_tracks_for_ids,
            list(profile.get("offline_track_ids") or profile.get("library_track_ids") or []),
            history_pool_cap,
        ),
        "albums": outer_executor.submit(
            timed_call_fn,
            build_album_items_fn,
            server,
            profile,
        ),
    }

    collaborative_tracks: List[Dict[str, Any]] = []
    fallback_tracks: List[Dict[str, Any]] = []
    artist_neighbor_candidates: List[Dict[str, Any]] = []
    anchor_neighbor_candidates: List[Dict[str, Any]] = []
    primary_anchor_candidates: List[Dict[str, Any]] = []
    rediscovery_neighbor_candidates: List[Dict[str, Any]] = []
    rediscovery_tracks: List[Dict[str, Any]] = []
    offline_tracks: List[Dict[str, Any]] = []
    album_items: List[Dict[str, Any]] = []
    artist_items: List[Dict[str, Any]] = []
    artist_artifact_meta: Dict[str, Any] = {}
    stage_timings = dict(preparation.stage_timings)

    for future_name, future in fetch_futures.items():
        try:
            payload = future.result()
        except Exception:
            payload = ({} if future_name == "anchor_neighbors" else [], 0)
        if future_name == "anchor_neighbors":
            anchor_payload, elapsed_ms = payload
            anchor_payload = dict(anchor_payload or {})
            anchor_neighbor_candidates = list(anchor_payload.get("anchor_neighbors") or [])
            primary_anchor_candidates = list(anchor_payload.get("primary_anchor_neighbors") or [])
            rediscovery_neighbor_candidates = list(anchor_payload.get("rediscovery_neighbors") or [])
            stage_timings["anchor_neighbor_ms"] = int(elapsed_ms or 0)
            continue
        result, elapsed_ms = payload
        stage_key = {
            "collaborative": "collaborative_fetch_ms",
            "fallback": "fallback_fetch_ms",
            "artist_artifacts": "artist_neighbor_ms",
            "rediscovery_tracks": "rediscovery_fetch_ms",
            "offline_tracks": "offline_fetch_ms",
            "albums": "album_pool_ms",
        }.get(future_name)
        if stage_key:
            stage_timings[stage_key] = int(elapsed_ms or 0)
        if future_name == "collaborative":
            collaborative_tracks = list(result or [])
        elif future_name == "fallback":
            fallback_tracks = list(result or [])
        elif future_name == "artist_artifacts":
            artifact_payload = dict(result or {})
            artist_neighbor_candidates = list(
                artifact_payload.get("neighbor_candidates") or []
            )
            artist_items = list(artifact_payload.get("artists") or [])
            artist_artifact_meta = dict(artifact_payload.get("meta") or {})
        elif future_name == "rediscovery_tracks":
            rediscovery_tracks = list(result or [])
        elif future_name == "offline_tracks":
            offline_tracks = list(result or [])
        elif future_name == "albums":
            album_items = list(result or [])

    return SnapshotFetchResults(
        collaborative_tracks=collaborative_tracks,
        fallback_tracks=fallback_tracks,
        artist_neighbor_candidates=artist_neighbor_candidates,
        anchor_neighbor_candidates=anchor_neighbor_candidates,
        primary_anchor_candidates=primary_anchor_candidates,
        rediscovery_neighbor_candidates=rediscovery_neighbor_candidates,
        rediscovery_tracks=rediscovery_tracks,
        offline_tracks=offline_tracks,
        album_items=album_items,
        artist_items=artist_items,
        artist_artifact_meta=artist_artifact_meta,
        stage_timings=stage_timings,
    )


def assemble_snapshot_payload(
    *,
    server: Any,
    profile: Dict[str, Any],
    started_at: float,
    preparation: SnapshotPreparation,
    fetched: SnapshotFetchResults,
    history_pool_cap: int,
    home_fallback_pool_cap: int,
    home_pool_candidate_cap: int,
    track_list_to_candidates_fn: Callable[..., List[Dict[str, Any]]],
    trim_candidate_pool_fn: Callable[..., List[Dict[str, Any]]],
    build_feature_aware_pools_fn: Callable[..., Dict[str, List[Dict[str, Any]]]],
    combine_pools_fn: Callable[..., Tuple[List[Dict[str, Any]], Dict[str, int]]],
    prefilter_pool_order_fn: Callable[[str], Sequence[str]],
    post_filter_row_candidates_fn: Callable[..., List[Dict[str, Any]]],
    prefiltered_pool_name_fn: Callable[[str], str],
    build_profile_allocator_features_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    summarize_snapshot_pool_features_fn: Callable[..., Dict[str, Any]],
) -> Dict[str, Any]:
    exploration_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(fetched.collaborative_tracks[history_pool_cap // 2:] or []),
            *(fetched.fallback_tracks[home_fallback_pool_cap // 3:] or []),
            *((candidate.get("track") or {}) for candidate in fetched.artist_neighbor_candidates[:18]),
        ],
        history_pool_cap,
    )

    pools: Dict[str, List[Dict[str, Any]]] = {
        "history_recent": trim_candidate_pool_fn(
            server,
            track_list_to_candidates_fn(
                server,
                preparation.recent_history_tracks,
                generator_name="history_recent",
                base_score=5.0,
                reason="From your recent listening history.",
            ),
            limit=home_pool_candidate_cap,
        ),
        "history_top": trim_candidate_pool_fn(
            server,
            track_list_to_candidates_fn(
                server,
                preparation.top_history_tracks,
                generator_name="history_top",
                base_score=5.1,
                reason="You come back to these often.",
            ),
            limit=home_pool_candidate_cap,
        ),
        "offline_library": trim_candidate_pool_fn(
            server,
            track_list_to_candidates_fn(
                server,
                fetched.offline_tracks,
                generator_name="offline_library",
                base_score=4.6,
                reason="Ready when you need it offline.",
            ),
            limit=home_pool_candidate_cap,
        ),
        "collaborative": trim_candidate_pool_fn(
            server,
            track_list_to_candidates_fn(
                server,
                fetched.collaborative_tracks,
                generator_name="collaborative_home",
                base_score=4.8,
                reason="Listeners with similar taste also played this.",
            ),
            limit=home_pool_candidate_cap,
        ),
        "artist_neighbors": trim_candidate_pool_fn(
            server,
            fetched.artist_neighbor_candidates,
            limit=home_pool_candidate_cap,
        ),
        "anchor_neighbors": trim_candidate_pool_fn(
            server,
            fetched.anchor_neighbor_candidates,
            limit=home_pool_candidate_cap,
        ),
        "primary_anchor_neighbors": trim_candidate_pool_fn(
            server,
            fetched.primary_anchor_candidates,
            limit=home_pool_candidate_cap,
        ),
        "taste_fallback": trim_candidate_pool_fn(
            server,
            track_list_to_candidates_fn(
                server,
                fetched.fallback_tracks,
                generator_name="taste_fallback",
                base_score=2.6,
                reason="Taste-filtered fallback while the feed warms up.",
            ),
            limit=home_pool_candidate_cap,
        ),
        "rediscovery": trim_candidate_pool_fn(
            server,
            [
                *track_list_to_candidates_fn(
                    server,
                    fetched.rediscovery_tracks,
                    generator_name="rediscovery_history",
                    base_score=4.2,
                    reason="A favorite worth bringing back.",
                ),
                *fetched.rediscovery_neighbor_candidates,
            ],
            limit=home_pool_candidate_cap,
        ),
        "exploration": trim_candidate_pool_fn(
            server,
            track_list_to_candidates_fn(
                server,
                exploration_tracks,
                generator_name="exploration_pool",
                base_score=2.9,
                reason="A measured step outside your usual rotation.",
            ),
            limit=home_pool_candidate_cap,
        ),
    }
    feature_pool_started_at = time.perf_counter()
    pools.update(
        build_feature_aware_pools_fn(
            server,
            profile,
            pools,
        )
    )
    stage_timings = dict(fetched.stage_timings)
    stage_timings["feature_pool_ms"] = int(
        (time.perf_counter() - feature_pool_started_at) * 1000
    )
    for row_kind in ("quiet_picks", "deep_cuts"):
        prefiltered_candidates, _ = combine_pools_fn(
            server,
            {"pools": pools},
            prefilter_pool_order_fn(row_kind),
            limit=max(home_pool_candidate_cap * 2, 72),
        )
        prefiltered_candidates = post_filter_row_candidates_fn(
            server,
            row_kind,
            profile,
            prefiltered_candidates,
        )
        if prefiltered_candidates:
            pools[prefiltered_pool_name_fn(row_kind)] = trim_candidate_pool_fn(
                server,
                prefiltered_candidates,
                limit=max(home_pool_candidate_cap, 56),
            )
    profile_allocator_features = build_profile_allocator_features_fn(profile)
    pool_allocator_features = summarize_snapshot_pool_features_fn(
        server,
        profile=profile,
        pools=pools,
    )
    total_ms = int((time.perf_counter() - started_at) * 1000)
    return {
        "generated_at": time.time(),
        "build_ms": total_ms,
        "pool_counts": {
            key: len(value or [])
            for key, value in pools.items()
        },
        "row_mode": "candidate_snapshot_v42",
        "albums_count": len(fetched.album_items),
        "artists_count": len(fetched.artist_items),
        "primary_anchor_id": server._recommendation_trim_text(
            (preparation.primary_anchor_track or {}).get("id")
        ),
        "quiet_seed": server._recommendation_quiet_base_query(profile),
        "profile_allocator_features": profile_allocator_features,
        "pool_allocator_features": pool_allocator_features,
        "stage_timings_ms": stage_timings,
        "pools": pools,
        "albums": fetched.album_items,
        "artists": fetched.artist_items,
        "artist_artifact_meta": dict(fetched.artist_artifact_meta or {}),
    }
