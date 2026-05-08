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
def _recommendation_required_row_fallback_seed(
    row_kind: str,
    snapshot: Dict[str, Any] | None,
    profile: Dict[str, Any] | None = None,
):
    from auralis_backend.recommend.row_seed_builder import (
        build_required_fallback_seed as _home_build_required_fallback_seed,
    )

    resolved_profile = dict(profile or {})
    if not resolved_profile:
        resolved_profile = {
            "collaborative": ((snapshot or {}).get("collaborative") or {}),
        }
    seed = _home_build_required_fallback_seed(
        server=sys.modules[__name__],
        row_kind=row_kind,
        profile=resolved_profile,
        snapshot=dict(snapshot or {}),
    )
    if isinstance(seed, dict):
        return seed
    fallback_tracks = _recommendation_home_fallback_tracks(
        resolved_profile,
        limit=24,
    )
    fallback_candidates = _recommendation_candidates_from_tracks(
        fallback_tracks,
        f"required_fallback:{row_kind}",
        2.25,
        "Fallback picks while personalization data is warming up.",
    )

    if row_kind == "trending_for_you":
        candidates = []
        collaborative_ids = (
            (resolved_profile.get("collaborative") or {}).get("candidate_track_ids") or []
        )
        if collaborative_ids:
            collaborative_tracks = _recommendation_fetch_tracks_for_ids(
                collaborative_ids,
                limit=12,
            )
            candidates.extend(
                _recommendation_candidates_from_tracks(
                    collaborative_tracks,
                    "trending_required:collaborative",
                    3.4,
                    "Trending among listeners similar to you.",
                )
            )
        candidates.extend(
            _recommendation_candidates_from_tracks(
                fallback_tracks,
                "trending_required:fallback",
                2.4,
                "Trending picks filtered by your profile.",
            )
        )
        return {
            "title": "Trending for you",
            "kind": "trending_for_you",
            "candidates": candidates,
            "row_strategy": "hybrid" if collaborative_ids else "fallback",
            "fallback_reason": "required_row_missing",
        }

    if row_kind == "quiet_picks":
        quiet_query = _recommendation_quiet_base_query(resolved_profile)
        return {
            "title": "Quiet picks",
            "kind": "quiet_picks",
            "quiet_query": quiet_query,
            "used_queries": [quiet_query] if quiet_query else [],
            "candidates": fallback_candidates,
            "row_strategy": "fallback",
            "fallback_reason": "required_row_missing",
        }

    if row_kind == "continue_listening":
        return {
            "title": "Continue the vibe",
            "kind": "continue_listening",
            "candidates": fallback_candidates,
            "row_strategy": "fallback",
            "fallback_reason": "required_row_missing",
        }

    if row_kind == "because_you_played":
        return {
            "title": "Because you played recently",
            "kind": "because_you_played",
            "candidates": fallback_candidates,
            "row_strategy": "fallback",
            "fallback_reason": "required_row_missing",
        }

    return None


@_with_server_globals
def _recommendation_apply_quiet_row_runtime_fields(finalized, row_seed):
    from auralis_backend.recommend.row_item_finalizer import (
        apply_quiet_row_runtime_fields as _home_apply_quiet_row_runtime_fields,
    )

    return _home_apply_quiet_row_runtime_fields(
        server=sys.modules[__name__],
        finalized=dict(finalized or {}),
        row_seed=dict(row_seed or {}),
    )



@_with_server_globals
def _recommendation_track_text(track: Optional[Dict[str, Any]]) -> str:
    if not isinstance(track, dict):
        return ""
    title = _recommendation_trim_text(track.get("title")) or "Unknown Track"
    artist = _recommendation_trim_text(
        track.get("channel") or track.get("author") or track.get("artist")
    ) or "Unknown Artist"
    album = _recommendation_trim_text(track.get("album") or track.get("album_title"))
    parts = [f"track {title}", f"artist {artist}"]
    if album:
        parts.append(f"album {album}")
    return ". ".join(parts)


@_with_server_globals
def _recommendation_artist_text(artist: Optional[Dict[str, Any]]) -> str:
    if not isinstance(artist, dict):
        return ""
    name = _recommendation_trim_text(artist.get("name")) or "Unknown Artist"
    description = _recommendation_trim_text(
        artist.get("description")
        or artist.get("subtitle")
        or artist.get("type")
    )
    if description:
        return f"artist {name}. {description}"
    return f"artist {name}"


@_with_server_globals
def _recommendation_album_text(album: Optional[Dict[str, Any]]) -> str:
    if not isinstance(album, dict):
        return ""
    title = _recommendation_trim_text(album.get("title")) or "Unknown Album"
    artist = _recommendation_trim_text(album.get("artist")) or "Unknown Artist"
    year = _recommendation_trim_text(str(album.get("year") or ""))
    parts = [f"album {title}", f"artist {artist}"]
    if year:
        parts.append(f"year {year}")
    return ". ".join(parts)


@_with_server_globals
def _recommendation_artist_embedding_key(artist: Optional[Dict[str, Any]]) -> str:
    if not isinstance(artist, dict):
        return ""
    artist_id = _recommendation_trim_text(artist.get("id") or artist.get("browseId"))
    if artist_id:
        return f"artist:{artist_id}"
    name = _recommendation_trim_text(artist.get("name"))
    if not name:
        return ""
    return f"artist:{_normalize_text(name)}"


@_with_server_globals
def _recommendation_album_embedding_key(album: Optional[Dict[str, Any]]) -> str:
    if not isinstance(album, dict):
        return ""
    album_id = _recommendation_trim_text(album.get("id") or album.get("browseId"))
    if album_id:
        return f"album:{album_id}"
    title = _recommendation_trim_text(album.get("title"))
    artist = _recommendation_trim_text(album.get("artist"))
    if not title and not artist:
        return ""
    return f"album:{_normalize_text(title)}|{_normalize_text(artist)}"


@_with_server_globals
def _recommendation_track_embedding_key(track: Optional[Dict[str, Any]]) -> str:
    if not isinstance(track, dict):
        return ""
    track_id = _recommendation_trim_text(track.get("id") or track.get("videoId"))
    if track_id:
        return f"track:{track_id}"
    title = _recommendation_trim_text(track.get("title"))
    artist = _recommendation_trim_text(
        track.get("channel") or track.get("author") or track.get("artist")
    )
    album = _recommendation_trim_text(track.get("album") or track.get("album_title"))
    if not any([title, artist, album]):
        return ""
    return f"track:{_normalize_text(title)}|{_normalize_text(artist)}|{_normalize_text(album)}"


@_with_server_globals
def _recommendation_text_embedding_key(label: str, value: str) -> str:
    normalized = _normalize_text(value)
    if not normalized:
        return ""
    return f"{label}:{hashlib.sha1(normalized.encode('utf-8')).hexdigest()}"


@_with_server_globals
def _recommendation_cached_embedding(namespace: str, key: str):
    if not namespace or not key:
        return None
    return _cache_lookup(
        recommendation_embedding_cache,
        recommendation_embedding_lock,
        namespace,
        key,
    )


@_with_server_globals
def _recommendation_store_embedding(namespace: str, key: str, value: List[float]):
    if not namespace or not key or not value:
        return
    _cache_store(
        recommendation_embedding_cache,
        recommendation_embedding_lock,
        namespace,
        key,
        list(value),
        RECOMMENDATION_EMBED_CACHE_TTL_SECONDS,
    )


@_with_server_globals
def _recommendation_embed_entries(namespace: str, keyed_texts):
    results = {}
    pending = []
    for key, text in keyed_texts:
        if not key or not text:
            continue
        cached = _recommendation_cached_embedding(namespace, key)
        if cached is not None:
            results[key] = list(cached)
            continue
        pending.append((key, text))

    if pending:
        embeddings = _assistant_embed_texts([text for _, text in pending])
        for (key, _text), embedding in zip(pending, embeddings):
            normalized = _vector_normalize(embedding)
            if normalized:
                _recommendation_store_embedding(namespace, key, normalized)
                results[key] = normalized

    return results


@_with_server_globals
def _recommendation_track_embeddings(tracks):
    keyed_texts = []
    key_order = []
    for track in tracks or []:
        key = _recommendation_track_embedding_key(track)
        text = _recommendation_track_text(track)
        if not key or not text:
            continue
        keyed_texts.append((key, text))
        key_order.append((key, track))
    embeddings = _recommendation_embed_entries("track", keyed_texts)
    resolved = {}
    for key, track in key_order:
        resolved[key] = embeddings.get(key) or []
    return resolved


@_with_server_globals
def _recommendation_artist_embeddings(artists):
    keyed_texts = []
    key_order = []
    for artist in artists or []:
        key = _recommendation_artist_embedding_key(artist)
        text = _recommendation_artist_text(artist)
        if not key or not text:
            continue
        keyed_texts.append((key, text))
        key_order.append((key, artist))
    embeddings = _recommendation_embed_entries("artist", keyed_texts)
    resolved = {}
    for key, artist in key_order:
        resolved[key] = embeddings.get(key) or []
    return resolved


@_with_server_globals
def _recommendation_album_embeddings(albums):
    keyed_texts = []
    key_order = []
    for album in albums or []:
        key = _recommendation_album_embedding_key(album)
        text = _recommendation_album_text(album)
        if not key or not text:
            continue
        keyed_texts.append((key, text))
        key_order.append((key, album))
    embeddings = _recommendation_embed_entries("album", keyed_texts)
    resolved = {}
    for key, album in key_order:
        resolved[key] = embeddings.get(key) or []
    return resolved


@_with_server_globals
def _recommendation_trim_text(value: Optional[str]) -> str:
    return (value or "").strip()


@_with_server_globals
def _recommendation_unique_strings(values, limit: Optional[int] = None):
    ordered = []
    seen = set()
    for raw in values or []:
        value = _recommendation_trim_text(raw)
        normalized = _normalize_text(value)
        if not value or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(value)
        if limit is not None and len(ordered) >= limit:
            break
    return ordered


@_with_server_globals
def _recommendation_unique_track_ids(values, limit: Optional[int] = None):
    ordered = []
    seen = set()
    for raw in values or []:
        value = _recommendation_trim_text(raw)
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
        if limit is not None and len(ordered) >= limit:
            break
    return ordered


@_with_server_globals
def _recommendation_unique_snapshot_tracks(values, limit: Optional[int] = None):
    ordered = []
    seen = set()
    for raw in values or []:
        if not isinstance(raw, dict):
            continue
        normalized = normalize_recommendation_track(raw)
        if normalized is None:
            continue
        track_id = _recommendation_trim_text(normalized.get("id"))
        if not track_id or track_id in seen:
            continue
        seen.add(track_id)
        ordered.append(normalized)
        if limit is not None and len(ordered) >= limit:
            break
    return ordered


@_with_server_globals
def _recommendation_track_from_details(payload: Dict[str, Any]):
    if not isinstance(payload, dict):
        return None
    video_id = (
        payload.get("video_id")
        or payload.get("id")
        or payload.get("videoId")
    )
    if not video_id:
        return None
    return {
        "id": str(video_id),
        "title": payload.get("title") or "Unknown Track",
        "duration": parse_duration_seconds(payload.get("duration")),
        "thumbnail": payload.get("thumbnail"),
        "channel": payload.get("author") or payload.get("artist") or "Unknown Artist",
        "album": payload.get("album") or payload.get("album_title"),
        "album_id": payload.get("album_id"),
    }


@_with_server_globals
def _recommendation_track_from_song_payload(payload: Dict[str, Any]):
    if not isinstance(payload, dict):
        return None
    video_details = payload.get("videoDetails") or {}
    track_payload = normalize_recommendation_track(
        {
            "id": payload.get("videoId") or video_details.get("videoId"),
            "videoId": video_details.get("videoId"),
            "title": video_details.get("title"),
            "lengthSeconds": video_details.get("lengthSeconds"),
            "author": video_details.get("author"),
            "thumbnail": extract_thumbnail(video_details),
            "album": (extract_album_info(payload) or {}).get("title")
            or (extract_album_info(video_details) or {}).get("title"),
            "album_id": (extract_album_info(payload) or {}).get("id")
            or (extract_album_info(video_details) or {}).get("id"),
        }
    )
    if track_payload is None:
        return None
    return track_payload


@_with_server_globals
def _recommendation_fetch_track_for_id_lightweight(track_id: str):
    normalized_id = _recommendation_trim_text(track_id)
    if not normalized_id:
        return None
    song_payload = _upstream_call_with_retry(
        lambda: ytmusic.get_song(normalized_id),
        attempts=UPSTREAM_RETRY_ATTEMPTS,
        backoff_seconds=UPSTREAM_RETRY_BACKOFF_SECONDS,
        default={},
    )
    track = _recommendation_track_from_song_payload(song_payload or {})
    if track is not None:
        return track
    return None


@_with_server_globals
def _recommendation_cached_track(track_id: str):
    return _cache_lookup_recommendation_track_detail(
        _recommendation_trim_text(track_id)
    )


@_with_server_globals
def _recommendation_store_cached_track(track_id: str, track: Dict[str, Any]):
    normalized_id = _recommendation_trim_text(track_id)
    if not normalized_id:
        return
    _cache_store_recommendation_track_detail(
        normalized_id,
        track,
        ttl_seconds=RECOMMENDATION_TRACK_CACHE_TTL_SECONDS,
    )


@_with_server_globals
def _recommendation_fetch_track_for_id(track_id: str):
    cached = _recommendation_cached_track(track_id)
    if cached is not None:
        return cached
    lightweight = _recommendation_fetch_track_for_id_lightweight(track_id)
    if lightweight is not None:
        _recommendation_store_cached_track(track_id, lightweight)
        return lightweight
    try:
        details = _assistant_tool_get_track_details(track_id)
    except Exception:
        details = {}
    track = _recommendation_track_from_details(details)
    if track is not None:
        _recommendation_store_cached_track(track_id, track)
    return track




