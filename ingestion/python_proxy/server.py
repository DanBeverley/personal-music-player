from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
import json
import os
import re
import time

import requests
import yt_dlp
from ytmusicapi import YTMusic

def extract_thumbnail(data):
    if not data: return None
    video_id = data.get("videoId") or data.get("video_id") or data.get("id")
    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    thumbs = data.get("thumbnails") or data.get("thumbnail")
    if isinstance(thumbs, list) and len(thumbs) > 0:
        return thumbs[-1].get("url")
    elif isinstance(thumbs, dict):
        return thumbs.get("url")
    return None

def extract_artist(data):
    if not data: return "Unknown Artist"
    artists = data.get("artists") or []
    if artists and isinstance(artists, list):
        return ", ".join([a.get("name", "") for a in artists])
    author = data.get("author")
    if author:
        return author.get("name") if isinstance(author, dict) else str(author)
    uploader = data.get("uploader")
    return uploader if uploader else "Unknown Artist"

def extract_album_info(data):
    if not data:
        return None

    album = data.get("album")
    if isinstance(album, dict):
        title = album.get("name") or album.get("title")
        album_id = album.get("id") or album.get("browseId")
        if title:
            return {"id": album_id, "title": title}
    elif isinstance(album, str) and album.strip():
        return {"id": None, "title": album.strip()}

    albums = data.get("albums")
    if isinstance(albums, list) and albums:
        first = albums[0]
        if isinstance(first, dict):
            title = first.get("name") or first.get("title")
            album_id = first.get("id") or first.get("browseId")
            if title:
                return {"id": album_id, "title": title}
        elif isinstance(first, str) and first.strip():
            return {"id": None, "title": first.strip()}
    return None

def normalize_album_results(raw_results):
    albums = []
    seen = set()
    for entry in raw_results or []:
        result_type = (entry.get("resultType") or entry.get("type") or "").lower()
        browse_id = entry.get("browseId") or entry.get("id")
        if result_type and result_type != "album":
            continue
        if not browse_id or browse_id in seen:
            continue
        seen.add(browse_id)
        albums.append({
            "id": browse_id,
            "title": entry.get("title"),
            "artist": extract_artist(entry),
            "thumbnail": extract_thumbnail(entry),
            "year": entry.get("year") or "",
            "track_count": entry.get("trackCount") or entry.get("track_count") or 0,
        })
    return albums

def _normalize_song_result(entry):
    if not entry:
        return None
    video_id = entry.get("videoId") or entry.get("video_id") or entry.get("id")
    if not video_id:
        return None
    album_info = extract_album_info(entry) or {}
    return {
        "id": video_id,
        "title": entry.get("title"),
        "duration": parse_duration_seconds(
            entry.get("duration_seconds")
            or entry.get("lengthSeconds")
            or entry.get("length")
            or entry.get("duration")
        ),
        "thumbnail": extract_thumbnail(entry),
        "channel": extract_artist(entry),
        "album": album_info.get("title"),
        "album_id": album_info.get("id"),
    }

def _ytmusic_song_search(query: str, limit: int):
    results = []
    seen = set()

    def add_entry(entry):
        normalized = _normalize_song_result(entry)
        if not normalized:
            return
        track_id = normalized["id"]
        if not track_id or track_id in seen:
            return
        seen.add(track_id)
        results.append(normalized)

    try:
        raw_results = ytmusic.search(query, filter="songs", limit=limit)
    except Exception:
        raw_results = []

    for entry in raw_results:
        add_entry(entry)
        if len(results) >= limit:
            return results

    if len(results) < limit:
        try:
            fallback_results = ytmusic.search(query, limit=max(limit * 3, 12))
        except Exception:
            fallback_results = []

        for entry in fallback_results:
            result_type = (entry.get("resultType") or entry.get("type") or "").lower()
            if result_type and result_type not in {"song", "video"}:
                continue
            add_entry(entry)
            if len(results) >= limit:
                break

    return results

def _ytdlp_song_search(query: str, limit: int):
    url = f"ytsearch{limit}:{query}"
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'skip_download': True,
    }
    results = []
    seen = set()
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return results

    for entry in info.get("entries", []) or []:
        video_id = entry.get("id") or entry.get("url")
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        results.append({
            "id": video_id,
            "title": entry.get("title"),
            "duration": parse_duration_seconds(entry.get("duration")),
            "thumbnail": extract_thumbnail(entry),
            "channel": extract_artist(entry),
            "album": None,
            "album_id": None,
        })
        if len(results) >= limit:
            break
    return results

def lookup_album_for_song(video_id: str, title: str, artist: str):
    candidates = []
    try:
        raw_results = ytmusic.search(f"{title} {artist}".strip(), filter="songs", limit=6)
    except Exception:
        raw_results = []

    for entry in raw_results:
        album_info = extract_album_info(entry)
        if not album_info or not album_info.get("title"):
            continue
        score = 0
        if entry.get("videoId") == video_id:
            score += 4
        if title and entry.get("title", "").strip().lower() == title.strip().lower():
            score += 2
        entry_artist = extract_artist(entry)
        if artist and entry_artist and artist.strip().lower() in entry_artist.strip().lower():
            score += 2
        if album_info.get("id"):
            score += 1
        candidates.append((score, album_info))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]

def parse_duration_seconds(value):
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        parts = [int(part) for part in text.split(":") if part.isdigit()]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0

def normalize_recommendation_track(data):
    if not data:
        return None
    video_id = data.get("videoId") or data.get("video_id") or data.get("id")
    if not video_id:
        return None
    album_info = extract_album_info(data) or {}
    return {
        "id": video_id,
        "title": data.get("title"),
        "duration": parse_duration_seconds(
            data.get("duration_seconds")
            or data.get("lengthSeconds")
            or data.get("length")
            or data.get("duration")
        ),
        "thumbnail": extract_thumbnail(data),
        "channel": extract_artist(data),
        "album": album_info.get("title"),
        "album_id": album_info.get("id"),
    }

def _normalize_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())

def _query_tokens(query: str):
    return [
        token
        for token in re.split(r"[^a-z0-9]+", _normalize_text(query))
        if len(token) >= 3
    ]

app = FastAPI(title="Auralis Proxy & Recommendation Engine")

# Allow Flutter emulator to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
ytmusic = YTMusic()
STREAM_INFO_TTL_SECONDS = 21600
STREAM_WARM_CHUNK_BYTES = int(os.environ.get("STREAM_WARM_CHUNK_BYTES", "786432"))
STREAM_CHUNK_TTL_SECONDS = 1800
STREAM_WARM_WORKERS = int(os.environ.get("STREAM_WARM_WORKERS", "8"))
PREPARE_SESSION_WORKERS = int(os.environ.get("PREPARE_SESSION_WORKERS", "4"))
PREPARE_SESSION_MAX_LOOKAHEAD = int(os.environ.get("PREPARE_SESSION_MAX_LOOKAHEAD", "18"))
stream_info_cache = {}
stream_info_inflight = {}
stream_info_lock = Lock()
stream_chunk_cache = {}
stream_chunk_lock = Lock()
stream_chunk_inflight = {}
stream_chunk_inflight_lock = Lock()
prepare_metrics = deque(maxlen=180)
prepare_metrics_lock = Lock()
stream_warm_executor = ThreadPoolExecutor(max_workers=STREAM_WARM_WORKERS)
upstream_http = requests.Session()

class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    seed_id: str = None
    seed_ids: List[str] = Field(default_factory=list)
    taste_queries: List[str] = Field(default_factory=list)
    artist_hints: List[str] = Field(default_factory=list)
    avoid_ids: List[str] = Field(default_factory=list)

class DownloadRequest(BaseModel):
    video_id: str
    title: str = ""

class WarmStreamRequest(BaseModel):
    video_ids: List[str] = Field(default_factory=list)
    current_video_id: Optional[str] = None
    active_queue: bool = False
    lookahead: int = 0

def _extract_stream_info(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'no_warnings': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    headers = {}
    for key, value in (info.get("http_headers") or {}).items():
        if key and value:
            headers[str(key)] = str(value)

    return {
        "url": info["url"],
        "headers": headers,
        "mime_type": info.get("ext") or info.get("acodec") or "audio/mp4",
        "duration": info.get("duration") or 0,
    }

def _is_stream_info_cached(video_id: str):
    now = time.time()
    with stream_info_lock:
        cached = stream_info_cache.get(video_id)
        return bool(cached and cached["expires_at"] > now)

def _extract_total_length(headers):
    content_range = headers.get("content-range") or headers.get("Content-Range")
    if content_range and "/" in content_range:
        total = content_range.split("/")[-1].strip()
        if total.isdigit():
            return int(total)

    content_length = headers.get("content-length") or headers.get("Content-Length")
    if content_length and content_length.isdigit():
        return int(content_length)
    return None

def _get_cached_stream_chunk(video_id: str, min_bytes: int = 0):
    now = time.time()
    with stream_chunk_lock:
        cached = stream_chunk_cache.get(video_id)
        if cached and cached["expires_at"] > now:
            payload = cached["payload"]
            if len(payload.get("bytes") or b"") >= min_bytes:
                return payload
            return None
        if cached:
            stream_chunk_cache.pop(video_id, None)
    return None

def _store_stream_chunk(video_id: str, payload):
    with stream_chunk_lock:
        stream_chunk_cache[video_id] = {
            "payload": payload,
            "expires_at": time.time() + STREAM_CHUNK_TTL_SECONDS,
        }

def _chunk_target_bytes(position: int, active_queue: bool):
    if active_queue:
        if position <= 0:
            return 2097152
        if position == 1:
            return 1572864
        if position == 2:
            return 1048576
        return STREAM_WARM_CHUNK_BYTES
    if position <= 0:
        return 1048576
    if position == 1:
        return 786432
    return STREAM_WARM_CHUNK_BYTES

def _parse_byte_range(range_header: Optional[str], total_length: Optional[int]):
    if not range_header:
        return None

    value = range_header.strip().lower()
    if not value.startswith("bytes="):
        return None

    spec = value.split("=", 1)[1].strip()
    if "," in spec or "-" not in spec:
        return None

    start_text, end_text = spec.split("-", 1)
    start_text = start_text.strip()
    end_text = end_text.strip()

    if not start_text:
        if total_length is None or not end_text.isdigit():
            return None
        suffix_length = int(end_text)
        if suffix_length <= 0:
            return None
        start = max(total_length - suffix_length, 0)
        end = total_length - 1
        return start, end

    if not start_text.isdigit():
        return None

    start = int(start_text)
    end = None
    if end_text:
        if not end_text.isdigit():
            return None
        end = int(end_text)

    if total_length is not None:
        if start >= total_length:
            return None
        if end is None or end >= total_length:
            end = total_length - 1

    if end is not None and end < start:
        return None

    return start, end

def _record_prepare_metric(video_id: str, metrics: dict):
    with prepare_metrics_lock:
        prepare_metrics.append({
            "video_id": video_id,
            "resolve_ms": metrics.get("resolve_ms") or 0,
            "chunk_ms": metrics.get("chunk_ms") or 0,
            "server_ms": metrics.get("server_ms") or 0,
            "cached_prefix_bytes": metrics.get("cached_prefix_bytes") or 0,
            "resolve_cache_hit": bool(metrics.get("resolve_cache_hit")),
            "chunk_cache_hit": bool(metrics.get("chunk_cache_hit")),
            "active_queue": bool(metrics.get("active_queue")),
            "created_at": time.time(),
        })

def _summarize_prepare_metrics():
    with prepare_metrics_lock:
        items = list(prepare_metrics)

    if not items:
        return {
            "sample_count": 0,
            "avg_resolve_ms": 0,
            "avg_chunk_ms": 0,
            "avg_server_ms": 0,
            "avg_cached_prefix_bytes": 0,
            "resolve_cache_hit_rate": 0.0,
            "chunk_cache_hit_rate": 0.0,
        }

    count = len(items)
    return {
        "sample_count": count,
        "avg_resolve_ms": int(sum(item["resolve_ms"] for item in items) / count),
        "avg_chunk_ms": int(sum(item["chunk_ms"] for item in items) / count),
        "avg_server_ms": int(sum(item["server_ms"] for item in items) / count),
        "avg_cached_prefix_bytes": int(
            sum(item["cached_prefix_bytes"] for item in items) / count
        ),
        "resolve_cache_hit_rate": round(
            sum(1 for item in items if item["resolve_cache_hit"]) / count, 3
        ),
        "chunk_cache_hit_rate": round(
            sum(1 for item in items if item["chunk_cache_hit"]) / count, 3
        ),
    }

def _warm_initial_stream_chunk(video_id: str, stream_info: dict, target_bytes: int):
    target_bytes = max(target_bytes, STREAM_WARM_CHUNK_BYTES)
    while True:
        cached = _get_cached_stream_chunk(video_id, min_bytes=target_bytes)
        if cached is not None:
            return cached

        with stream_chunk_inflight_lock:
            pending = stream_chunk_inflight.get(video_id)
            if pending is None:
                pending = Future()
                stream_chunk_inflight[video_id] = pending
                should_fetch = True
            else:
                should_fetch = False

        if not should_fetch:
            try:
                pending.result(timeout=25)
            except Exception:
                pass
            continue

        try:
            existing = _get_cached_stream_chunk(video_id)
            existing_bytes = existing.get("bytes") if existing else b""
            total_length = existing.get("total_length") if existing else None
            if total_length is not None and len(existing_bytes) >= total_length:
                pending.set_result(existing)
                return existing

            headers = dict(stream_info["headers"])
            start_offset = len(existing_bytes)
            headers["range"] = f"bytes={start_offset}-{max(target_bytes - 1, start_offset)}"
            req = upstream_http.get(
                stream_info["url"],
                headers=headers,
                stream=True,
                timeout=(5, 20),
            )
            try:
                req.raise_for_status()
                chunks = [existing_bytes] if existing_bytes else []
                total_bytes = len(existing_bytes)
                for chunk in req.iter_content(chunk_size=1024 * 64):
                    if not chunk:
                        continue
                    remaining = target_bytes - total_bytes
                    if remaining <= 0:
                        break
                    if len(chunk) > remaining:
                        chunk = chunk[:remaining]
                    chunks.append(chunk)
                    total_bytes += len(chunk)
                    if total_bytes >= target_bytes:
                        break

                payload = {
                    "bytes": b"".join(chunks),
                    "content_type": req.headers.get(
                        "content-type",
                        stream_info.get("mime_type") or "audio/mp4",
                    ),
                    "total_length": _extract_total_length(req.headers) or total_length,
                }
                _store_stream_chunk(video_id, payload)
                pending.set_result(payload)
                return payload
            finally:
                req.close()
        except Exception as exc:
            pending.set_exception(exc)
            raise
        finally:
            with stream_chunk_inflight_lock:
                if stream_chunk_inflight.get(video_id) is pending:
                    stream_chunk_inflight.pop(video_id, None)

def _prepare_stream_track(video_id: str, target_chunk_bytes: int, active_queue: bool):
    total_start = time.perf_counter()
    resolve_start = time.perf_counter()
    resolve_cache_hit = _is_stream_info_cached(video_id)
    stream_info = get_stream_info(video_id)
    resolve_ms = int((time.perf_counter() - resolve_start) * 1000)

    chunk_cache_hit = _get_cached_stream_chunk(video_id, min_bytes=target_chunk_bytes) is not None
    chunk_start = time.perf_counter()
    chunk_payload = _warm_initial_stream_chunk(video_id, stream_info, target_chunk_bytes)
    chunk_ms = int((time.perf_counter() - chunk_start) * 1000)

    metrics = {
        "prepared": True,
        "playback_path": f"/proxy_stream/{video_id}",
        "resolve_cache_hit": resolve_cache_hit,
        "chunk_cache_hit": chunk_cache_hit,
        "resolve_ms": resolve_ms,
        "chunk_ms": chunk_ms,
        "target_chunk_bytes": target_chunk_bytes,
        "cached_prefix_bytes": len(chunk_payload.get("bytes") or b""),
        "duration": stream_info.get("duration") or 0,
        "active_queue": active_queue,
        "server_ms": int((time.perf_counter() - total_start) * 1000),
    }
    _record_prepare_metric(video_id, metrics)
    return metrics

def _prepare_stream_track_safely(video_id: str, target_chunk_bytes: int, active_queue: bool):
    try:
        return _prepare_stream_track(video_id, target_chunk_bytes, active_queue)
    except Exception:
        return None

def _prepare_streams(
    video_ids: List[str],
    limit: int = 18,
    current_video_id: Optional[str] = None,
    active_queue: bool = False,
):
    prepared = {}
    deduped_ids = []
    seen = set()
    for video_id in video_ids:
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        deduped_ids.append(video_id)
        if len(deduped_ids) >= limit:
            break

    if not deduped_ids:
        return prepared

    if current_video_id and current_video_id in deduped_ids:
        deduped_ids.remove(current_video_id)
        deduped_ids.insert(0, current_video_id)

    targets = {
        video_id: _chunk_target_bytes(index, active_queue)
        for index, video_id in enumerate(deduped_ids)
    }

    max_workers = min(PREPARE_SESSION_WORKERS, len(deduped_ids))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _prepare_stream_track,
                video_id,
                targets[video_id],
                active_queue,
            ): video_id
            for video_id in deduped_ids
        }
        for future, video_id in future_map.items():
            try:
                prepared[video_id] = future.result(timeout=25)
            except Exception:
                continue
    return prepared

def get_stream_info(video_id: str):
    now = time.time()
    with stream_info_lock:
        cached = stream_info_cache.get(video_id)
        if cached and cached["expires_at"] > now:
            return cached["payload"]

        pending = stream_info_inflight.get(video_id)
        if pending is None:
            pending = Future()
            stream_info_inflight[video_id] = pending
            should_extract = True
        else:
            should_extract = False

    if not should_extract:
        return pending.result(timeout=25)

    try:
        payload = _extract_stream_info(video_id)
        with stream_info_lock:
            stream_info_cache[video_id] = {
                "payload": payload,
                "expires_at": now + STREAM_INFO_TTL_SECONDS,
            }
        pending.set_result(payload)
        return payload
    except Exception as exc:
        pending.set_exception(exc)
        raise
    finally:
        with stream_info_lock:
            if stream_info_inflight.get(video_id) is pending:
                stream_info_inflight.pop(video_id, None)

def _warm_stream_safely(video_id: str):
    _prepare_stream_track_safely(
        video_id,
        _chunk_target_bytes(4, False),
        False,
    )

def queue_stream_warmup(video_ids: List[str], limit: int = 18):
    seen = set()
    for video_id in video_ids:
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        stream_warm_executor.submit(_warm_stream_safely, video_id)
        if len(seen) >= limit:
            break

@app.post("/prepare_session")
def prepare_session(req: WarmStreamRequest):
    start_time = time.perf_counter()
    limit = min(max(req.lookahead or len(req.video_ids), 1), PREPARE_SESSION_MAX_LOOKAHEAD)
    prepared = _prepare_streams(
        req.video_ids,
        limit=limit,
        current_video_id=req.current_video_id,
        active_queue=req.active_queue,
    )
    return {
        "status": "success",
        "prepared": prepared,
        "prepared_ids": list(prepared.keys()),
        "summary": _summarize_prepare_metrics(),
        "server_ms": int((time.perf_counter() - start_time) * 1000),
    }

@app.get("/latency_summary")
def latency_summary():
    return {
        "status": "success",
        "summary": _summarize_prepare_metrics(),
        "stream_info_cache_size": len(stream_info_cache),
        "stream_chunk_cache_size": len(stream_chunk_cache),
    }

@app.get("/")
def health_check():
    return {"status": "Auralis Python Proxy is running"}

@app.post("/track_details")
def get_track_details(req: DownloadRequest):
    try:
        video_id = req.video_id
        
        release_date = ""
        artist = ""
        album_title = ""
        album_id = None
        
        try:
            song = ytmusic.get_song(video_id)
            if "microformat" in song and "microformatDataRenderer" in song["microformat"]:
                release_date = song["microformat"]["microformatDataRenderer"].get("publishDate", "")
            vd = song.get("videoDetails", {})
            artist = vd.get("author", "")
            song_album = extract_album_info(song) or extract_album_info(vd)
            if song_album:
                album_title = song_album.get("title") or ""
                album_id = song_album.get("id")
        except Exception:
            pass
            
        watch = ytmusic.get_watch_playlist(videoId=video_id)
        video_details = watch.get("videoDetails", {})
        track_title = video_details.get("title") or ""
        if not artist:
            artist = extract_artist(video_details)
        if not album_title:
            looked_up_album = lookup_album_for_song(video_id, track_title, artist)
            if looked_up_album:
                album_title = looked_up_album.get("title") or ""
                album_id = looked_up_album.get("id")
        
        similar_tracks = []
        for track in watch.get("tracks", []):
            if track.get("videoId") == video_id or not track.get("videoId"):
                continue
            similar_tracks.append({
                "id": track["videoId"],
                "title": track.get("title"),
                "duration": track.get("length") or track.get("duration_seconds") or 0,
                "thumbnail": extract_thumbnail(track),
                "channel": extract_artist(track),
                "album": (extract_album_info(track) or {}).get("title"),
                "album_id": (extract_album_info(track) or {}).get("id"),
            })
        return {
            "status": "success",
            "video_id": video_id,
            "title": track_title,
            "author": artist,
            "thumbnail": extract_thumbnail(video_details),
            "duration": video_details.get("lengthSeconds"),
            "release_date": release_date,
            "album": album_title,
            "album_title": album_title,
            "album_id": album_id,
            "lyrics_available": bool(watch.get("lyrics")),
            "similar_tracks": similar_tracks
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/lyrics/{video_id}")
def get_track_lyrics(video_id: str):
    try:
        watch = ytmusic.get_watch_playlist(videoId=video_id)
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

        lyrics_payload = ytmusic.get_lyrics(lyrics_browse_id, timestamps=True)
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/search")
def search_youtube(req: SearchRequest):
    query = req.query
    results = []

    # Check if the query is a direct YouTube URL
    url_match = re.search(r"(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})", query)
    if url_match:
        video_id = url_match.group(1)
        try:
            watch = ytmusic.get_watch_playlist(videoId=video_id)
            vd = watch.get("videoDetails", {})
            results.append({
                "id": video_id,
                "title": vd.get("title") or "Unknown URL Track",
                "duration": vd.get("lengthSeconds") or 0,
                "thumbnail": extract_thumbnail(vd),
                "channel": extract_artist(vd)
            })
            return {"status": "success", "results": results}
        except Exception:
            pass # Fallback to normal search if extraction fails

    results = _ytmusic_song_search(query, req.limit)
    if not results:
        results = _ytdlp_song_search(query, req.limit)

    return {"status": "success", "results": results}

@app.post("/search_albums")
def search_albums(req: SearchRequest):
    try:
        raw_results = ytmusic.search(req.query, filter="albums", limit=req.limit)
        albums = normalize_album_results(raw_results)
        if not albums:
            try:
                fallback_results = ytmusic.search(req.query, limit=max(req.limit * 3, 12))
            except Exception:
                fallback_results = []
            albums = normalize_album_results(fallback_results)
        return {"status": "success", "albums": albums}
    except Exception as e:
        return {"status": "success", "albums": []}

@app.get("/album/{album_id}")
def get_album_details(album_id: str):
    try:
        album = ytmusic.get_album(album_id)
        album_thumbnail = extract_thumbnail(album)
        album_artist = extract_artist(album)
        tracks = []

        for entry in album.get("tracks", []):
            video_id = entry.get("videoId")
            if not video_id:
                continue
            tracks.append({
                "id": video_id,
                "title": entry.get("title"),
                "duration": parse_duration_seconds(
                    entry.get("duration_seconds")
                    or entry.get("duration")
                    or entry.get("length")
                ),
                "thumbnail": extract_thumbnail(entry) or album_thumbnail,
                "channel": extract_artist(entry) or album_artist,
                "album": album.get("title"),
                "album_title": album.get("title"),
                "album_id": album_id,
            })

        return {
            "status": "success",
            "id": album_id,
            "title": album.get("title"),
            "artist": album_artist,
            "thumbnail": album_thumbnail,
            "year": album.get("year") or "",
            "track_count": len(tracks),
            "tracks": tracks,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/suggest")
def get_suggestions(req: SearchRequest):
    try:
        suggestions = ytmusic.get_search_suggestions(req.query)
        # ytmusicapi returns a list of dictionaries like {"text": "querystring"} or just strings
        results = [s.get("text", s) if isinstance(s, dict) else s for s in suggestions]
        return {"status": "success", "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def _fallback_home_candidates(limit: int):
    results = []
    try:
        home_feed = ytmusic.get_home(limit=limit)
    except Exception:
        home_feed = []

    for carousel in home_feed:
        for item in carousel.get("contents", []):
            track = normalize_recommendation_track(item)
            if track is None:
                continue
            results.append((track, 1.0))
            if len(results) >= limit + 10:
                return results

    if len(results) < 5:
        try:
            backup = ytmusic.search("trending hit songs", filter="songs", limit=limit)
        except Exception:
            backup = []
        for entry in backup:
            track = normalize_recommendation_track(entry)
            if track is None:
                continue
            results.append((track, 0.7))
            if len(results) >= limit + 10:
                break
    return results

def _rank_recommendation_candidates(
    candidates,
    *,
    limit: int,
    avoid_ids,
    seed_ids,
    artist_hints,
    taste_queries,
):
    ranked = {}
    blocked_ids = {item for item in avoid_ids if item}.union(
        {item for item in seed_ids if item}
    )
    normalized_artist_hints = [
        _normalize_text(item) for item in artist_hints if _normalize_text(item)
    ]
    query_token_groups = [
        tokens[:4]
        for tokens in (_query_tokens(item) for item in taste_queries)
        if tokens
    ]

    for track, base_score in candidates:
        track_id = track.get("id")
        if not track_id or track_id in blocked_ids:
            continue
        title_text = _normalize_text(track.get("title"))
        artist_text = _normalize_text(track.get("channel"))
        album_text = _normalize_text(track.get("album"))
        score = float(base_score)

        for index, artist_hint in enumerate(normalized_artist_hints):
            if artist_hint and artist_hint in artist_text:
                score += max(3.2 - (index * 0.35), 0.8)

        for index, tokens in enumerate(query_token_groups):
            hits = sum(
                1
                for token in tokens
                if token in title_text or token in artist_text or token in album_text
            )
            if hits:
                score += min(hits, 2) * max(2.1 - (index * 0.18), 0.45)

        existing = ranked.get(track_id)
        if existing is None or score > existing["score"]:
            ranked[track_id] = {
                "track": track,
                "score": score,
                "artist_key": artist_text,
            }

    ordered = sorted(
        ranked.values(),
        key=lambda item: (item["score"], item["track"].get("title") or ""),
        reverse=True,
    )

    results = []
    artist_counts = {}
    for item in ordered:
        artist_key = item["artist_key"]
        if artist_key:
            artist_count = artist_counts.get(artist_key, 0)
            if artist_count >= 2 and len(results) + 1 < limit:
                continue
            artist_counts[artist_key] = artist_count + 1
        results.append(item["track"])
        if len(results) >= limit:
            break
    return results

@app.post("/recommend")
def get_recommendations(req: SearchRequest):
    """
    Builds recommendations from multiple explicit taste signals instead of
    relying on only the latest played seed.
    """
    try:
        seed_ids = []
        for candidate in [req.seed_id, *(req.seed_ids or [])]:
            if candidate and candidate not in seed_ids:
                seed_ids.append(candidate)

        candidates = []

        for index, seed_id in enumerate(seed_ids[:3]):
            if not seed_id or seed_id == "trending hit songs":
                continue
            try:
                mix = ytmusic.get_watch_playlist(
                    videoId=seed_id,
                    limit=max(req.limit, 10) + 6,
                )
            except Exception:
                continue

            source_weight = max(6.0 - index, 2.0)
            collected = 0
            for track in mix.get("tracks", []):
                normalized = normalize_recommendation_track(track)
                if normalized is None or normalized["id"] == seed_id:
                    continue
                candidates.append((normalized, source_weight))
                collected += 1
                if collected >= max(req.limit // 2, 6):
                    break

        if len(candidates) < max(req.limit, 10):
            for index, artist_hint in enumerate(req.artist_hints[:3]):
                query = (artist_hint or "").strip()
                if not query:
                    continue
                try:
                    results = ytmusic.search(f"{query} songs", filter="songs", limit=6)
                except Exception:
                    continue

                source_weight = max(3.8 - (index * 0.3), 1.3)
                for track in results:
                    normalized = normalize_recommendation_track(track)
                    if normalized is not None:
                        candidates.append((normalized, source_weight))

        if len(candidates) < max(req.limit, 10) and not seed_ids:
            for index, taste_query in enumerate(req.taste_queries[:2]):
                query = (taste_query or "").strip()
                if not query:
                    continue
                try:
                    results = ytmusic.search(query, filter="songs", limit=5)
                except Exception:
                    continue

                source_weight = max(2.7 - (index * 0.25), 0.9)
                for track in results:
                    normalized = normalize_recommendation_track(track)
                    if normalized is not None:
                        candidates.append((normalized, source_weight))

        if len(candidates) < max(req.limit, 10):
            candidates.extend(_fallback_home_candidates(req.limit))

        results = _rank_recommendation_candidates(
            candidates,
            limit=req.limit,
            avoid_ids=req.avoid_ids or [],
            seed_ids=seed_ids,
            artist_hints=req.artist_hints or [],
            taste_queries=req.taste_queries or [],
        )
        return {"status": "success", "recommendations": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/warm_streams")
def warm_streams(req: WarmStreamRequest):
    prepared = _prepare_streams(
        req.video_ids,
        limit=18,
        current_video_id=req.current_video_id,
        active_queue=req.active_queue,
    )
    return {"status": "success", "streams": prepared}

@app.post("/download")
def download_audio(req: DownloadRequest):
    out_path = os.path.join(DOWNLOAD_DIR, f"{req.video_id}.mp3")
    json_cache = os.path.join(DOWNLOAD_DIR, f"{req.video_id}.json")
    
    # Aggressive Cache Purging if Windows Host yt_dlp choked and left a 0-byte MP3 artifact
    if os.path.exists(out_path) and os.path.getsize(out_path) < 100:
        os.remove(out_path)
        if os.path.exists(json_cache):
            os.remove(json_cache)
            
    if os.path.exists(out_path) and os.path.exists(json_cache):
        try:
            with open(json_cache, "r") as f:
                meta = json.load(f)
                meta["message"] = "Already downloaded"
                return meta
        except Exception:
            pass
            
    if os.path.exists(out_path):
        ydl_opts = {'quiet': True, 'no_warnings': True}
        url = f"https://www.youtube.com/watch?v={req.video_id}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                meta = {
                    "status": "success", 
                    "video_id": req.video_id,
                    "title": info.get("title") or "Unknown Track",
                    "thumbnail": info.get("thumbnail"),
                    "duration": info.get("duration") or 0,
                    "filesize": os.path.getsize(out_path),
                    "filename": f"{req.video_id}.mp3",
                    "author": info.get("channel") or info.get("uploader"),
                    "message": "Already downloaded"
                }
                with open(json_cache, "w") as f:
                    json.dump(meta, f)
                return meta
            except Exception as e:
                # If extraction fails on an existing file, the file must be deleted to allow re-downloads!
                os.remove(out_path)
                raise HTTPException(status_code=500, detail=str(e))

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(DOWNLOAD_DIR, f"{req.video_id}.%(ext)s"),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': False,
        'no_warnings': True,
    }

    url = f"https://www.youtube.com/watch?v={req.video_id}"
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            # We first extract info to get the metadata for the UI (thumbnail, title, byte size)
            info = ydl.extract_info(url, download=True)
            meta = {
                "status": "success",
                "video_id": req.video_id,
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration") or 0,
                "filesize": info.get("filesize_approx") or info.get("filesize") or 0,
                "filename": f"{req.video_id}.mp3",
                "author": info.get("channel") or info.get("uploader"),
            }
        except Exception as e:
            if "unavailable" in str(e).lower() or "sign in" in str(e).lower():
                search_query = f"ytsearch1:{req.title} audio" if req.title else f"ytsearch1:{req.video_id} audio"
                info_list = ydl.extract_info(search_query, download=True)
                if "entries" in info_list and len(info_list["entries"]) > 0:
                    info = info_list["entries"][0]
                    meta = {
                        "status": "success",
                        "video_id": req.video_id,
                        "title": info.get("title"),
                        "thumbnail": info.get("thumbnail"),
                        "duration": info.get("duration") or 0,
                        "filesize": info.get("filesize_approx") or info.get("filesize") or 0,
                        "filename": f"{req.video_id}.mp3",
                        "author": info.get("channel") or info.get("uploader"),
                    }
                else:
                    raise HTTPException(status_code=500, detail=f"Fallback search failed for {req.video_id}")
            else:
                raise HTTPException(status_code=500, detail=str(e))
                
        try:
            with open(os.path.join(DOWNLOAD_DIR, f"{req.video_id}.json"), "w") as f:
                json.dump(meta, f)
        except Exception as e:
            print(f"JSON DUMP ERROR: {e}")
            pass
        return meta

@app.get("/stream/{video_id}")
def stream_audio(video_id: str):
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found. Please download first.")
    
    return FileResponse(file_path, media_type="audio/mpeg", filename=f"{video_id}.mp3")


@app.get("/proxy_stream/{video_id}")
def proxy_stream(video_id: str, request: Request):
    try:
        stream_info = get_stream_info(video_id)
        cached_chunk = _get_cached_stream_chunk(video_id)
        range_header = request.headers.get("range")

        if cached_chunk:
            cached_bytes = cached_chunk.get("bytes") or b""
            content_type = cached_chunk.get("content_type") or stream_info.get("mime_type") or "audio/mp4"
            total_length = cached_chunk.get("total_length")
            parsed_range = _parse_byte_range(range_header, total_length)

            if not range_header or parsed_range is not None:
                start = parsed_range[0] if parsed_range else 0
                end = parsed_range[1] if parsed_range else None

                if start < len(cached_bytes):
                    cached_end = len(cached_bytes) - 1
                    requested_end = end if end is not None else cached_end
                    slice_end = min(cached_end, requested_end)
                    cached_slice = cached_bytes[start:slice_end + 1]

                    def generate_cached_then_upstream():
                        if cached_slice:
                            yield cached_slice

                        upstream_start = len(cached_bytes)
                        if upstream_start <= start:
                            upstream_start = start

                        if end is not None and upstream_start > end:
                            return

                        if total_length is not None and upstream_start >= total_length:
                            return

                        upstream_headers = dict(stream_info["headers"])
                        if end is None:
                            upstream_headers["range"] = f"bytes={upstream_start}-"
                        else:
                            upstream_headers["range"] = f"bytes={upstream_start}-{end}"

                        req = upstream_http.get(
                            stream_info["url"],
                            headers=upstream_headers,
                            stream=True,
                            timeout=(5, 30),
                        )
                        try:
                            req.raise_for_status()
                            for chunk in req.iter_content(chunk_size=1024 * 64):
                                if chunk:
                                    yield chunk
                        finally:
                            req.close()

                    resp_headers = {"Accept-Ranges": "bytes"}
                    status_code = 206 if range_header else 200
                    if total_length is not None:
                        response_end = end if end is not None else total_length - 1
                        if status_code == 206:
                            resp_headers["Content-Range"] = (
                                f"bytes {start}-{response_end}/{total_length}"
                            )
                            resp_headers["Content-Length"] = str(
                                max(response_end - start + 1, 0)
                            )
                        else:
                            resp_headers["Content-Length"] = str(total_length)
                    elif status_code == 206 and end is not None:
                        resp_headers["Content-Length"] = str(
                            max(end - start + 1, 0)
                        )

                    return StreamingResponse(
                        generate_cached_then_upstream(),
                        status_code=status_code,
                        headers=resp_headers,
                        media_type=content_type,
                    )

        headers = dict(stream_info["headers"])
        if range_header:
            headers["range"] = range_header

        req = upstream_http.get(
            stream_info["url"],
            headers=headers,
            stream=True,
            timeout=(5, 30),
        )

        def generate_upstream():
            try:
                for chunk in req.iter_content(chunk_size=1024 * 64):
                    if chunk:
                        yield chunk
            finally:
                req.close()

        resp_headers = {}
        for k, v in req.headers.items():
            if k.lower() in ['content-type', 'content-length', 'content-range', 'accept-ranges']:
                resp_headers[k] = v
        return StreamingResponse(
            generate_upstream(),
            status_code=req.status_code,
            headers=resp_headers,
            media_type=req.headers.get("content-type", "audio/mp4")
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/direct_url/{video_id}")
def direct_stream_url(video_id: str):
    try:
        stream_info = get_stream_info(video_id)
        return {
            "status": "success",
            "url": stream_info["url"],
            "headers": stream_info["headers"],
            "mime_type": stream_info["mime_type"],
            "duration": stream_info["duration"],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting Auralis Proxy Server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
