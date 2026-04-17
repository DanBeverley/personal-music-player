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

    def get_track_lyrics(self, video_id: str):
        return get_track_lyrics(self._server, video_id)

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


def get_track_lyrics(server: Any, video_id: str):
    try:
        watch = server.ytmusic.get_watch_playlist(videoId=video_id)
        lyrics_browse_id = watch.get("lyrics")
        if not lyrics_browse_id:
            return {
                "status": "success",
                "video_id": video_id,
                "has_lyrics": False,
                "has_timestamps": False,
                "source": None,
                "lines": [],
            }

        lyrics_payload = server.ytmusic.get_lyrics(lyrics_browse_id, timestamps=True)
        if not lyrics_payload:
            return {
                "status": "success",
                "video_id": video_id,
                "has_lyrics": False,
                "has_timestamps": False,
                "source": None,
                "lines": [],
            }

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
        else:
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

        return {
            "status": "success",
            "video_id": video_id,
            "has_lyrics": bool(lines),
            "has_timestamps": bool(lyrics_payload.get("hasTimestamps")),
            "source": lyrics_payload.get("source"),
            "lines": lines,
        }
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
