from __future__ import annotations

from functools import wraps
from auralis_backend.runtime_context import resolve_server


def _with_server_globals(fn):
    @wraps(fn)
    def _wrapped(*args, **kwargs):
        server = resolve_server()
        for key, value in vars(server).items():
            if key.startswith('__'):
                continue
            globals()[key] = value
        return fn(*args, **kwargs)

    return _wrapped
@_with_server_globals
def _recommendation_collaborative_neighbor_tracks(track_id: str, limit: int = 12):
    normalized_track_id = _recommendation_trim_text(track_id)
    if not normalized_track_id:
        return []
    model = _recommendation_get_collaborative_model()
    if not isinstance(model, dict) or not model.get("ready"):
        return []
    neighbor_ids = [
        _recommendation_trim_text(item.get("track_id"))
        for item in (model.get("item_neighbors") or {}).get(normalized_track_id, [])[:limit]
        if _recommendation_trim_text(item.get("track_id"))
    ]
    return _recommendation_fetch_tracks_for_ids(neighbor_ids, limit=limit)


@_with_server_globals
def _recommendation_collaborative_track_scores(track, profile):
    from auralis_backend.domain.collaborative import track_scores
    import sys

    return track_scores(sys.modules[__name__], track, profile)



@_with_server_globals
def _recommendation_fetch_tracks_for_ids(track_ids, limit: int = 12):
    ordered_ids = []
    seen = set()
    lookup_cap = min(max(limit + RECOMMENDATION_TRACK_LOOKUP_EXTRA, limit), 48)
    for raw_id in track_ids or []:
        normalized_id = _recommendation_trim_text(raw_id)
        if not normalized_id or normalized_id in seen:
            continue
        seen.add(normalized_id)
        ordered_ids.append(normalized_id)
        if len(ordered_ids) >= lookup_cap:
            break

    if not ordered_ids:
        return []

    futures = {}
    resolved = {}
    deadline = time.time() + RECOMMENDATION_TRACK_FETCH_BUDGET_SECONDS
    for track_id in ordered_ids:
        if len(resolved) >= limit:
            break
        cached = _recommendation_cached_track(track_id)
        if cached is not None:
            resolved[track_id] = cached
            continue
        futures[track_id] = recommendation_executor.submit(
            _recommendation_fetch_track_for_id,
            track_id,
        )

    tracks = []
    for track_id in ordered_ids:
        if len(tracks) >= limit:
            break
        if time.time() >= deadline:
            break
        track = resolved.get(track_id)
        if track is None:
            future = futures.get(track_id)
            if future is not None:
                try:
                    remaining = max(deadline - time.time(), 0.0)
                    if remaining <= 0:
                        break
                    track = future.result(
                        timeout=min(
                            RECOMMENDATION_TRACK_FETCH_PER_FUTURE_TIMEOUT_SECONDS,
                            remaining,
                        )
                    )
                except Exception:
                    track = None
        if track is None:
            continue
        tracks.append(track)
    for future in futures.values():
        if not future.done():
            future.cancel()
    return tracks



@_with_server_globals
def _recommendation_candidate(track, generator_name: str, generator_score: float, reason: str):
    normalized = normalize_recommendation_track(track)
    if normalized is None:
        return None
    enriched = dict(normalized)
    enriched["generator_name"] = generator_name
    enriched["recommendation_reason"] = reason
    enriched["generator_score"] = float(generator_score)
    return {
        "track": enriched,
        "generator_name": generator_name,
        "generator_score": float(generator_score),
        "reason": reason,
    }


@_with_server_globals
def _recommendation_vector_similarities(candidate_vector, profile):
    from auralis_backend.recommend.row_ranking import vector_similarities
    import sys

    return vector_similarities(sys.modules[__name__], candidate_vector, profile)


@_with_server_globals
def _recommendation_track_score(candidate, profile, row_kind: str, candidate_vector=None):
    from auralis_backend.recommend.row_ranking import track_score
    import sys

    return track_score(
        sys.modules[__name__],
        candidate,
        profile,
        row_kind,
        candidate_vector,
    )


@_with_server_globals
def _recommendation_quality_floor(row_kind: str) -> float:
    from auralis_backend.recommend.row_ranking import quality_floor

    return quality_floor(row_kind)


@_with_server_globals
def _recommendation_track_signature(track) -> str:
    if not isinstance(track, dict):
        return ""
    track_id = _recommendation_trim_text(
        track.get("id")
        or track.get("videoId")
        or track.get("video_id")
    )
    if track_id:
        return f"id:{track_id}"
    title = _normalize_text(track.get("title") or track.get("name") or "")
    artist = _normalize_text(
        track.get("channel")
        or track.get("author")
        or track.get("artist")
        or ""
    )
    album = _normalize_text(
        track.get("album")
        or track.get("album_title")
        or ""
    )
    if not title and not artist:
        return ""
    return f"track:{title}|{artist}|{album}"


@_with_server_globals
def _recommendation_min_items(row_kind: str) -> int:
    from auralis_backend.recommend.row_ranking import min_items

    return min_items(row_kind)


@_with_server_globals
def _recommendation_is_query_derived_source(source_name: str) -> bool:
    from auralis_backend.recommend.row_ranking import is_query_derived_source
    import sys

    return is_query_derived_source(sys.modules[__name__], source_name)


@_with_server_globals
def _recommendation_finalize_row_items(
    row_kind: str,
    title: str,
    candidates,
    profile,
    used_track_ids,
    *,
    max_items: int = 18,
):
    from auralis_backend.recommend.row_item_finalizer import finalize_row_items
    import sys

    return finalize_row_items(
        server=sys.modules[__name__],
        row_kind=row_kind,
        title=title,
        candidates=candidates,
        profile=profile,
        used_track_ids=used_track_ids,
        max_items=max_items,
    )


@_with_server_globals
def _recommendation_candidates_from_tracks(tracks, generator_name: str, base_score: float, reason: str):
    candidates = []
    for track in tracks or []:
        candidate = _recommendation_candidate(track, generator_name, base_score, reason)
        if candidate is not None:
            candidates.append(candidate)
    return candidates





