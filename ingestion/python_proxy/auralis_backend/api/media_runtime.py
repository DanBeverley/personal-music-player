from __future__ import annotations

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

    def get_album_details(self, album_id: str):
        return get_album_details(self._server, album_id)

    def get_artist_details(self, artist_id: str):
        return get_artist_details(self._server, artist_id)


def prepare_session(server: Any, req: Any):
    start_time = time.perf_counter()
    limit = min(
        max(req.lookahead or len(req.video_ids), 1),
        server.PREPARE_SESSION_MAX_LOOKAHEAD,
    )
    prepared, failed = prepare_streams_with_failures(
        server,
        req.video_ids,
        limit=limit,
        current_video_id=req.current_video_id,
        active_queue=req.active_queue,
    )
    return {
        "status": "success",
        "prepared": prepared,
        "prepared_ids": list(prepared.keys()),
        "failed": failed,
        "failed_ids": list(failed.keys()),
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


def get_album_details(server: Any, album_id: str):
    try:
        return build_album_details_payload(server, album_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def get_artist_details(server: Any, artist_id: str):
    try:
        return build_artist_details_payload(server, artist_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
