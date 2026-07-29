from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from fastapi import HTTPException

from .stream_core_runtime import (
    prepare_streams_with_failures,
    summarize_prepare_metrics,
)
from ..details.detail_runtime import (
    build_album_details_payload,
    build_artist_details_payload,
    build_track_details_payload,
)
from ..domain.catalog import normalize_artist_name, normalize_track_title


class MediaService:
    def __init__(self, server: Any) -> None:
        self._server = server

    def health_check(self):
        return health_check(self._server)

    def latency_summary(self):
        return latency_summary(self._server)

    def prepare_session(self, req: Any):
        return prepare_session(self._server, req)

    def get_track_details(self, req: Any):
        return get_track_details(self._server, req)

    def get_track_lyrics(self, video_id: str, *, title: str = "", artist: str = ""):
        return get_track_lyrics(self._server, video_id, title=title, artist=artist)

    def get_track_lyrics_meaning(self, video_id: str, req: Any):
        return get_track_lyrics_meaning(self._server, video_id, req)

    def get_album_details(self, album_id: str):
        return get_album_details(self._server, album_id)

    def get_artist_details(self, artist_id: str):
        return get_artist_details(self._server, artist_id)


def prepare_session(server: Any, req: Any):
    start_time = time.perf_counter()
    if req.background:
        from ..discovery.source_registry import youtube_background_resolution_blocked

        if youtube_background_resolution_blocked(server):
            return {
                "status": "success",
                "prepared": {},
                "prepared_ids": [],
                "failed": {},
                "failed_ids": [],
                "background_skipped": True,
                "skip_reason": "youtube_temporarily_blocked",
                "summary": summarize_prepare_metrics(server),
                "server_ms": int((time.perf_counter() - start_time) * 1000),
            }
    requested_limit = max(req.lookahead or len(req.track_keys), 1)
    if req.background:
        requested_limit = min(
            requested_limit,
            int(getattr(server, "PREPARE_BACKGROUND_MAX_LOOKAHEAD", 3)),
        )
    limit = min(requested_limit, server.PREPARE_SESSION_MAX_LOOKAHEAD)
    from .stream_runtime import prepare_playback_tracks

    prepared, failed = prepare_playback_tracks(
        server,
        req.track_keys,
        limit=limit,
        current_track_key=req.current_track_key,
        active_queue=req.active_queue,
        defer_all_chunks=req.background,
    )
    if req.background and failed:
        from ..discovery.source_registry import record_youtube_background_failures

        record_youtube_background_failures(server, failed)
    return {
        "status": "success",
        "prepared": prepared,
        "prepared_ids": list(prepared.keys()),
        "failed": failed,
        "failed_ids": list(failed.keys()),
        "background_skipped": False,
        "summary": summarize_prepare_metrics(server),
        "server_ms": int((time.perf_counter() - start_time) * 1000),
    }


def latency_summary(server: Any):
    return {
        "status": "success",
        "summary": summarize_prepare_metrics(server),
        "stream_info_cache_size": len(server.stream_info_cache),
        "stream_chunk_cache_size": len(server.stream_chunk_cache),
    }


def health_check(_server: Any):
    return {"status": "Auralis Python Proxy is running"}


def get_track_details(server: Any, req: Any):
    try:
        return build_track_details_payload(server, req.video_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _lyrics_response(
    *,
    video_id: str,
    lines: list[dict[str, Any]] | None = None,
    has_timestamps: bool = False,
    source: Any = None,
    resolved_from: str = "video",
):
    return {
        "status": "success",
        "video_id": video_id,
        "has_lyrics": bool(lines),
        "has_timestamps": bool(has_timestamps),
        "source": source,
        "resolved_from": resolved_from,
        "lines": list(lines or []),
    }


def _lyrics_lines(lyrics_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not lyrics_payload:
        return []
    lines = []
    if lyrics_payload.get("hasTimestamps"):
        for index, line in enumerate(lyrics_payload.get("lyrics", [])):
            text = getattr(line, "text", "").strip()
            if not text:
                continue
            lines.append({
                "index": index,
                "text": text,
                "start_time_ms": getattr(line, "start_time", None),
                "end_time_ms": getattr(line, "end_time", None),
            })
        return lines
    raw_text = (lyrics_payload.get("lyrics") or "").splitlines()
    for index, line in enumerate(raw_text):
        text = line.strip()
        if not text:
            continue
        lines.append({
            "index": index,
            "text": text,
            "start_time_ms": None,
            "end_time_ms": None,
        })
    return lines


def _lyrics_for_video(server: Any, video_id: str, *, resolved_from: str = "video"):
    watch = server.ytmusic.get_watch_playlist(videoId=video_id)
    lyrics_browse_id = watch.get("lyrics")
    if not lyrics_browse_id:
        return _lyrics_response(video_id=video_id, resolved_from=resolved_from)
    lyrics_payload = server.ytmusic.get_lyrics(lyrics_browse_id, timestamps=True)
    lines = _lyrics_lines(lyrics_payload)
    return _lyrics_response(
        video_id=video_id,
        lines=lines,
        has_timestamps=bool((lyrics_payload or {}).get("hasTimestamps")),
        source=(lyrics_payload or {}).get("source"),
        resolved_from=resolved_from,
    )


def _fallback_lyrics_video_ids(server: Any, *, title: str, artist: str, current_video_id: str) -> list[str]:
    cleaned_title = normalize_track_title(title)
    cleaned_artist = normalize_artist_name(artist)
    if not cleaned_title:
        return []
    base_query = " ".join(part for part in (cleaned_artist, cleaned_title) if part).strip()
    if not base_query:
        return []
    queries = [
        base_query,
        f"{base_query} lyrics",
        " ".join(part for part in (cleaned_title, cleaned_artist, "lyrics") if part).strip(),
    ]
    results: list[dict[str, Any]] = []
    for query in dict.fromkeys(q for q in queries if q):
        for search_filter in ("songs", "videos"):
            try:
                raw_results = server.ytmusic.search(query, filter=search_filter, limit=6) or []
            except Exception:
                raw_results = []
            results.extend(item for item in raw_results if isinstance(item, dict))
    output: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("videoId") or item.get("id") or "").strip()
        if not candidate_id or candidate_id == current_video_id or candidate_id in output:
            continue
        candidate_title = normalize_track_title(item.get("title") or "")
        artists = item.get("artists") or []
        first_artist = artists[0].get("name") if artists and isinstance(artists[0], dict) else ""
        candidate_artist = normalize_artist_name(item.get("artist") or first_artist)
        if not (
            candidate_title == cleaned_title
            or cleaned_title in candidate_title
            or candidate_title in cleaned_title
        ):
            continue
        if cleaned_artist and candidate_artist and cleaned_artist not in candidate_artist and candidate_artist not in cleaned_artist:
            continue
        output.append(candidate_id)
    return output[:3]


def get_track_lyrics(server: Any, video_id: str, *, title: str = "", artist: str = ""):
    try:
        primary = _lyrics_for_video(server, video_id)
        if primary.get("has_lyrics"):
            return primary
        for candidate_id in _fallback_lyrics_video_ids(
            server,
            title=title,
            artist=artist,
            current_video_id=video_id,
        ):
            fallback = _lyrics_for_video(
                server,
                candidate_id,
                resolved_from="title_artist_fallback",
            )
            if fallback.get("has_lyrics"):
                fallback["requested_video_id"] = video_id
                return fallback
        return primary
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _lyrics_meaning_cache(server: Any) -> dict[str, dict[str, Any]]:
    cache = getattr(server, "lyrics_meaning_cache", None)
    if isinstance(cache, dict):
        return cache
    cache = {}
    setattr(server, "lyrics_meaning_cache", cache)
    return cache


def _trim_text(value: Any, limit: int = 220) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _request_lines(req: Any) -> list[dict[str, Any]]:
    raw_lines = getattr(req, "lines", None) or []
    output: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_lines):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        output.append({
            "index": int(raw.get("index") or index),
            "text": text,
        })
    return output


def _lyrics_hash(lines: list[dict[str, Any]]) -> str:
    text = "\n".join(str(line.get("text") or "").strip() for line in lines)
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _lyrics_context_text(lines: list[dict[str, Any]], *, max_chars: int = 3600) -> str:
    text = "\n".join(str(line.get("text") or "").strip() for line in lines)
    text = "\n".join(line for line in text.splitlines() if line.strip())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit("\n", 1)[0].strip()


def _safe_string_list(value: Any, *, limit: int = 5) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        text = _trim_text(item, 180)
        if text:
            output.append(text)
        if len(output) >= limit:
            break
    return output


def _fallback_meaning_payload(
    *,
    video_id: str,
    title: str,
    artist: str,
    has_lyrics: bool,
    lyrics_hash: str,
) -> dict[str, Any]:
    track_label = " by ".join(part for part in (title or "this song", artist) if part)
    basis = "available lyrics and track metadata" if has_lyrics else "track metadata only"
    summary = (
        f"This is a quick context-based read of {track_label}. "
        "Neatie could not reach the full interpretation model, so treat this as a light starting point."
    )
    seed = (
        f"Let's discuss the meaning of {title or 'this song'}"
        f"{f' by {artist}' if artist else ''}. Start from the quick interpretation and help me go deeper."
    )
    return {
        "status": "success",
        "video_id": video_id,
        "cached": False,
        "summary": summary,
        "themes": ["Mood", "Story", "Personal interpretation"],
        "emotional_tone": "Uncertain, based on limited context.",
        "context_notes": [f"Interpretation basis: {basis}."],
        "notable_imagery": [],
        "confidence": 0.35 if not has_lyrics else 0.5,
        "source_notes": (
            "Generated without long lyric quotes. The model fallback did not use external context."
        ),
        "assistant_seed_message": seed,
        "lyrics_hash": lyrics_hash,
    }


def _meaning_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "themes": {"type": "array", "items": {"type": "string"}},
            "emotional_tone": {"type": "string"},
            "context_notes": {"type": "array", "items": {"type": "string"}},
            "notable_imagery": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
            "source_notes": {"type": "string"},
        },
        "required": [
            "summary",
            "themes",
            "emotional_tone",
            "context_notes",
            "notable_imagery",
            "confidence",
            "source_notes",
        ],
    }


def get_track_lyrics_meaning(server: Any, video_id: str, req: Any):
    requested_id = (video_id or getattr(req, "video_id", "") or "").strip()
    if not requested_id:
        raise HTTPException(status_code=400, detail="video_id is required")
    title = _trim_text(getattr(req, "title", ""), 180)
    artist = _trim_text(getattr(req, "artist", ""), 180)
    album = _trim_text(getattr(req, "album", ""), 180)
    year = _trim_text(getattr(req, "year", ""), 24)
    source = _trim_text(getattr(req, "source", ""), 120)
    lines = _request_lines(req)

    if not lines:
        try:
            lyrics_payload = get_track_lyrics(server, requested_id, title=title, artist=artist)
            lines = list(lyrics_payload.get("lines") or [])
            source = source or _trim_text(lyrics_payload.get("source"), 120)
        except Exception:
            lines = []

    lyrics_hash = _lyrics_hash(lines)
    cache_key = hashlib.sha256(
        json.dumps(
            {
                "video_id": requested_id,
                "title": title,
                "artist": artist,
                "lyrics_hash": lyrics_hash,
            },
            sort_keys=True,
        ).encode("utf-8", errors="ignore")
    ).hexdigest()
    cache = _lyrics_meaning_cache(server)
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        result = dict(cached)
        result["cached"] = True
        return result

    has_lyrics = bool(lines)
    lyrics_context = _lyrics_context_text(lines)
    metadata = ", ".join(
        part
        for part in (
            f"title: {title}" if title else "",
            f"artist: {artist}" if artist else "",
            f"album: {album}" if album else "",
            f"year: {year}" if year else "",
            f"lyrics source: {source}" if source else "",
        )
        if part
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are Neatie's Behind the Lyrics interpreter. "
                "Give a concise, careful interpretation of a song. "
                "Do not quote more than a few words from lyrics. "
                "Do not invent artist biography, historical events, or song intent as fact. "
                "Label uncertainty when context is limited. "
                "Return only JSON matching the schema."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Song metadata: {metadata or 'unknown'}.\n"
                f"Lyrics available: {'yes' if has_lyrics else 'no'}.\n"
                "Interpret the song for a listener who wants to understand the mood, themes, "
                "possible story, and imagery. Keep it brief and grounded.\n\n"
                f"Lyrics context:\n{lyrics_context or '[No lyrics available]'}"
            ),
        },
    ]

    try:
        call_structured = getattr(server, "_call_ollama_structured", None)
        if not callable(call_structured):
            raise RuntimeError("structured assistant runtime unavailable")
        raw = call_structured(
            messages,
            schema=_meaning_schema(),
            temperature=0.35,
            timeout_seconds=45,
            model_override=getattr(server, "OLLAMA_FAST_MODEL", None),
        )
        confidence = raw.get("confidence")
        try:
            confidence_value = float(confidence)
        except Exception:
            confidence_value = 0.68 if has_lyrics else 0.42
        confidence_value = max(0.0, min(1.0, confidence_value))
        seed = (
            f"I was listening to {title or 'this song'}"
            f"{f' by {artist}' if artist else ''}. "
            "Here is Neatie's quick interpretation:\n\n"
            f"{_trim_text(raw.get('summary'), 800)}\n\n"
            "Can we talk more about what this song might mean?"
        )
        result = {
            "status": "success",
            "video_id": requested_id,
            "cached": False,
            "summary": _trim_text(raw.get("summary"), 900),
            "themes": _safe_string_list(raw.get("themes"), limit=6),
            "emotional_tone": _trim_text(raw.get("emotional_tone"), 240),
            "context_notes": _safe_string_list(raw.get("context_notes"), limit=5),
            "notable_imagery": _safe_string_list(raw.get("notable_imagery"), limit=5),
            "confidence": confidence_value,
            "source_notes": _trim_text(raw.get("source_notes"), 260),
            "assistant_seed_message": seed,
            "lyrics_hash": lyrics_hash,
        }
    except Exception:
        result = _fallback_meaning_payload(
            video_id=requested_id,
            title=title,
            artist=artist,
            has_lyrics=has_lyrics,
            lyrics_hash=lyrics_hash,
        )

    cache[cache_key] = dict(result)
    if len(cache) > 256:
        for key in list(cache.keys())[:64]:
            cache.pop(key, None)
    return result


def get_album_details(server: Any, album_id: str):
    try:
        normalized_id = str(album_id or "").strip()
        prefix = "musicbrainz:release-group:"
        if normalized_id.startswith(prefix):
            from ..recommend.store_runtime import open_recommendation_store_connection

            release_group_id = normalized_id[len(prefix):]
            connection = open_recommendation_store_connection(server)
            try:
                row = connection.execute(
                    """
                    SELECT payload_json
                    FROM catalog_entities
                    WHERE entity_type = 'album' AND entity_key = ?
                    LIMIT 1
                    """,
                    [f"musicbrainz:release:{release_group_id}"],
                ).fetchone()
            finally:
                connection.close()
            if row is None:
                raise HTTPException(status_code=404, detail="Canonical album is not prepared")
            payload = json.loads(str(row[0] or "{}"))
            tracks = [
                dict(item)
                for item in payload.get("tracks") or []
                if isinstance(item, dict)
                and item.get("track_key")
                and item.get("playable") is not False
            ]
            if not tracks:
                raise HTTPException(status_code=404, detail="Canonical album has no playable tracklist")
            return {
                **payload,
                "status": "success",
                "id": normalized_id,
                "track_count": len(tracks),
                "tracks": tracks,
            }
        return build_album_details_payload(server, album_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def get_artist_details(server: Any, artist_id: str):
    try:
        return build_artist_details_payload(server, artist_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
