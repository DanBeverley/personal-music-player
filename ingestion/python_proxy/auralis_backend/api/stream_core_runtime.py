from __future__ import annotations

import json
import os
import shutil
import time
import urllib.parse
import re
from threading import BoundedSemaphore
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, List, Optional
from contextlib import contextmanager

import yt_dlp
import yt_dlp.plugins
from yt_dlp.cookies import YoutubeDLCookieJar

from .stream_cache import get_stream_cache, iter_and_store_full_stream

_capture_cache_slots = BoundedSemaphore(2)


def cache_prepared_track(server, video_id):
    cache = get_stream_cache(server)
    if not cache.enabled:
        return False
    if cache.head(video_id):
        return True
    with server.stream_info_lock:
        pending = server.stream_info_inflight.get(f'cache:{video_id}')
    if pending is not None:
        try:
            return bool(pending.result(timeout=75))
        except Exception:
            return False
    if not _capture_cache_slots.acquire(blocking=False):
        return False
    try:
        info = selected_stream_info(server, video_id)
        if not info:
            return False
        for _ in iter_and_store_full_stream(server, video_id,
                iter_upstream_stream(server, video_id, {**info, 'pinned': True}),
                content_type=info.get('mime_type', 'audio/mp4'),
                capture_duration=info.get('duration', 0) if info.get('compatibility') else None):
            pass
        return bool(cache.head(video_id))
    except Exception as exc:
        print(f'[EBB:stream] video={video_id} event=queue_cache_failed error={type(exc).__name__}')
        return False
    finally:
        _capture_cache_slots.release()


def _cache_capture(server, video_id, info):
    if not info.get('compatibility') or not hasattr(server, 'stream_warm_executor'):
        return
    retry_key = f'capture_retry:{video_id}'
    with server.stream_info_lock:
        retry = server.stream_info_cache.get(retry_key)
        if retry and retry['expires_at'] > time.time():
            return
    if not _capture_cache_slots.acquire(blocking=False):
        return
    key = f'cache:{video_id}'
    with server.stream_info_lock:
        if key in server.stream_info_inflight:
            _capture_cache_slots.release()
            return
        pending = Future()
        server.stream_info_inflight[key] = pending
    def store():
        stored = False
        try:
            cache = get_stream_cache(server)
            if cache.enabled and not cache.head(video_id):
                for _ in iter_and_store_full_stream(server, video_id,
                        iter_upstream_stream(server, video_id, info),
                        content_type=info['mime_type'],
                        capture_duration=info.get('duration', 0)):
                    pass
            stored = bool(cache.head(video_id)) if cache.enabled else False
        except Exception as exc:
            print(f'[EBB:stream] video={video_id} event=capture_cache_failed error={type(exc).__name__}')
        finally:
            pending.set_result(stored)
            with server.stream_info_lock:
                server.stream_info_inflight.pop(key, None)
                # Avoid retry storms, but do not permanently lose capture
                # persistence after a transient upload/transport failure.
                server.stream_info_cache[retry_key] = {
                    'expires_at': time.time() + (300 if stored else 30),
                    'payload': {},
                }
                for old in list(server.stream_info_cache):
                    if (old.startswith('capture_retry:')
                            and server.stream_info_cache[old]['expires_at'] <= time.time()):
                        server.stream_info_cache.pop(old, None)
            _capture_cache_slots.release()
    try:
        server.stream_warm_executor.submit(store)
    except Exception:
        pending.set_result(False)
        with server.stream_info_lock:
            server.stream_info_inflight.pop(key, None)
        _capture_cache_slots.release()

_STREAM_RESOLUTION_NAMESPACE = "playback_stream_resolution"
_STREAM_RESOLUTION_MODEL = "youtube"
_STREAM_RESOLUTION_EXPIRY_MARGIN_SECONDS = 90

# yt-dlp discovers plugin modules lazily. Recommendation verification uses
# several worker threads, and concurrent first-use discovery can register the
# same PO-token provider twice. Import-time initialization runs under Python's
# module lock and makes every later YoutubeDL construction read-only here.
yt_dlp.plugins.load_all_plugins()


def _resolved_stream_expiry(server: Any, payload: dict) -> tuple[float, bool]:
    now = time.time()
    memory_expiry = now + max(
        int(getattr(server, "STREAM_INFO_TTL_SECONDS", 21600) or 21600),
        60,
    )
    try:
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(str(payload.get("url") or "")).query
        )
        signed_expiry = float((query.get("expire") or [0])[0] or 0)
    except (TypeError, ValueError):
        signed_expiry = 0.0
    if signed_expiry <= now + _STREAM_RESOLUTION_EXPIRY_MARGIN_SECONDS:
        return memory_expiry, False
    return (
        min(
            memory_expiry,
            signed_expiry - _STREAM_RESOLUTION_EXPIRY_MARGIN_SECONDS,
        ),
        True,
    )


def _load_persisted_stream_info(server: Any, video_id: str):
    from ..recommend.store_runtime import (
        open_recommendation_store_connection_without_init,
    )

    connection = None
    try:
        connection = open_recommendation_store_connection_without_init(server)
        row = connection.execute(
            """
            SELECT model_id, payload_json
            FROM recommendation_feature_store
            WHERE namespace = ? AND entity_id = ?
            LIMIT 1
            """,
            (_STREAM_RESOLUTION_NAMESPACE, video_id),
        ).fetchone()
        if row is None or str(row["model_id"] or "") != _STREAM_RESOLUTION_MODEL:
            return None
        stored = json.loads(str(row["payload_json"] or "{}"))
        expires_at = float(stored.get("expires_at") or 0)
        stream_info = stored.get("stream_info")
        if (
            expires_at <= time.time() + _STREAM_RESOLUTION_EXPIRY_MARGIN_SECONDS
            or not isinstance(stream_info, dict)
            or not str(stream_info.get("url") or "").startswith(("http://", "https://"))
        ):
            connection.execute(
                """
                DELETE FROM recommendation_feature_store
                WHERE namespace = ? AND entity_id = ?
                """,
                (_STREAM_RESOLUTION_NAMESPACE, video_id),
            )
            connection.commit()
            return None
        payload = dict(stream_info)
        payload["expires_at"] = expires_at
        return payload, expires_at
    except Exception:
        return None
    finally:
        if connection is not None:
            connection.close()


def _persist_stream_info(
    server: Any,
    video_id: str,
    payload: dict,
    expires_at: float,
) -> None:
    from ..recommend.store_runtime import (
        open_recommendation_store_connection_without_init,
    )

    connection = None
    try:
        connection = open_recommendation_store_connection_without_init(server)
        connection.execute(
            """
            INSERT INTO recommendation_feature_store (
                namespace,
                entity_id,
                model_id,
                payload_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(namespace, entity_id) DO UPDATE SET
                model_id = excluded.model_id,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                _STREAM_RESOLUTION_NAMESPACE,
                video_id,
                _STREAM_RESOLUTION_MODEL,
                json.dumps(
                    {
                        "stream_info": payload,
                        "expires_at": expires_at,
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                time.time(),
            ),
        )
        connection.commit()
    except Exception:
        return
    finally:
        if connection is not None:
            connection.close()


def _delete_persisted_stream_info(server: Any, video_id: str) -> None:
    from ..recommend.store_runtime import (
        open_recommendation_store_connection_without_init,
    )

    connection = None
    try:
        connection = open_recommendation_store_connection_without_init(server)
        connection.execute(
            """
            DELETE FROM recommendation_feature_store
            WHERE namespace = ? AND entity_id = ?
            """,
            (_STREAM_RESOLUTION_NAMESPACE, video_id),
        )
        connection.commit()
    except Exception:
        return
    finally:
        if connection is not None:
            connection.close()


def _stream_failure_payload(exc: Exception):
    classified = classify_stream_failure(exc)
    return {
        **classified,
        "created_at": time.time(),
    }


def _get_stream_failure(server: Any, video_id: str):
    now = time.time()
    cooldown = int(getattr(server, "STREAM_FAILURE_COOLDOWN_SECONDS", 900) or 900)
    with server.stream_failure_lock:
        cached = server.stream_failure_cache.get(video_id)
        if cached and (now - float(cached.get("created_at") or 0.0)) < cooldown:
            return dict(cached)
        if cached:
            server.stream_failure_cache.pop(video_id, None)
    return None


def _store_stream_failure(server: Any, video_id: str, exc: Exception) -> dict:
    payload = _stream_failure_payload(exc)
    with server.stream_failure_lock:
        server.stream_failure_cache[video_id] = payload
    return payload


def _clear_stream_failure(server: Any, video_id: str) -> None:
    with server.stream_failure_lock:
        server.stream_failure_cache.pop(video_id, None)


class _QuietYtDlpLogger:
    """yt-dlp failures are summarized by the caller, not printed per video."""

    def debug(self, _message: str) -> None:
        return None

    def warning(self, _message: str) -> None:
        return None

    def error(self, _message: str) -> None:
        return None


def _ytdlp_opts(server: Any) -> dict:
    opts = {
        "format": "bestaudio[acodec!=none]/best[acodec!=none]/best",
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietYtDlpLogger(),
    }
    js_runtime = str(
        getattr(server, "AURALIS_YTDLP_JS_RUNTIME", "node") or "node"
    ).strip().lower()
    js_runtime_path = str(
        getattr(server, "AURALIS_YTDLP_JS_RUNTIME_PATH", "") or ""
    ).strip()
    resolved_runtime = js_runtime_path or shutil.which(js_runtime)
    if js_runtime and resolved_runtime:
        opts["js_runtimes"] = {
            js_runtime: {"path": resolved_runtime},
        }
    cookies_path = str(getattr(server, "AURALIS_YTDLP_COOKIES_PATH", "") or "").strip()
    if cookies_path:
        opts["cookiefile"] = cookies_path
    provider_url = str(
        getattr(server, "AURALIS_YTDLP_PO_PROVIDER_URL", "") or ""
    ).strip().rstrip("/")
    if provider_url:
        extractor_args = opts.setdefault("extractor_args", {})
        extractor_args.setdefault("youtube", {})["player_client"] = [
            "mweb",
            "web_safari",
        ]
        extractor_args["youtubepot-bgutilhttp"] = {
            "base_url": [provider_url],
        }
    return opts


def _mark_failure_stage(exc: Exception, stage: str) -> None:
    if not getattr(exc, "_auralis_failure_stage", None):
        exc._auralis_failure_stage = stage


def stream_failure_diagnostics(exc: Exception) -> dict:
    """Retain operational facts without URLs, provider messages or cookies."""
    stage = getattr(exc, "_auralis_failure_stage", "source_resolution")
    if stage not in {"cookie_load", "extraction", "media_preflight", "source_resolution"}:
        stage = "source_resolution"
    diagnostics = {"stage": stage, "exception_type": type(exc).__name__}
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int):
        diagnostics["errno"] = errno
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and 400 <= status <= 599:
        diagnostics["http_status"] = status
    return diagnostics


@contextmanager
def _ytdlp_extractor(options: dict):
    # cookiefile enables save-back on YoutubeDL.close. Deployment credentials
    # are read-only; each extractor instead owns a fresh, memory-only jar.
    options = dict(options)
    cookie_path = options.pop("cookiefile", None)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            if cookie_path:
                try:
                    cookies = YoutubeDLCookieJar(cookie_path)
                    cookies.load(ignore_discard=True, ignore_expires=True)
                    for cookie in cookies:
                        ydl.cookiejar.set_cookie(cookie)
                except Exception as exc:
                    _mark_failure_stage(exc, "cookie_load")
                    raise
            yield ydl
    except Exception as exc:
        _mark_failure_stage(exc, "extraction")
        raise


def extract_stream_info(server: Any, video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = _ytdlp_opts(server)
    try:
        with _ytdlp_extractor(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        if "requested format is not available" not in str(exc).casefold():
            raise
        # Some extractor clients expose usable formats but fail the selector.
        # Inspect the unprocessed result once and choose a real audio-bearing
        # URL instead of repeating the same failing format request.
        raw_opts = dict(ydl_opts)
        raw_opts.pop("format", None)
        with _ytdlp_extractor(raw_opts) as ydl:
            raw_info = ydl.extract_info(url, download=False, process=False)
        formats = [
            dict(entry)
            for entry in list((raw_info or {}).get("formats") or [])
            if isinstance(entry, dict)
            and entry.get("url")
            and str(entry.get("acodec") or "none").lower() != "none"
        ]
        if not formats:
            raise exc
        formats.sort(
            key=lambda entry: (
                1 if str(entry.get("vcodec") or "none").lower() == "none" else 0,
                1 if str(entry.get("ext") or "").lower() in {"m4a", "mp4"} else 0,
                float(entry.get("abr") or entry.get("tbr") or 0.0),
            ),
            reverse=True,
        )
        info = {**dict(raw_info or {}), **formats[0]}

    selected = next(
        (
            dict(entry)
            for entry in [
                *list((info or {}).get("requested_downloads") or []),
                *list((info or {}).get("requested_formats") or []),
                info,
            ]
            if isinstance(entry, dict) and entry.get("url")
        ),
        {},
    )
    if not selected:
        raise RuntimeError("stream_url_missing")

    headers = {}
    for key, value in {
        **dict((info or {}).get("http_headers") or {}),
        **dict(selected.get("http_headers") or {}),
    }.items():
        if key and value:
            headers[str(key)] = str(value)

    return {
        "url": selected["url"],
        "headers": headers,
        "mime_type": selected.get("ext") or selected.get("acodec") or "audio/mp4",
        "protocol": selected.get("protocol") or (info or {}).get("protocol"),
        "duration": selected.get("duration") or (info or {}).get("duration") or 0,
    }


def validate_stream_info(server: Any, video_id: str, stream_info: dict) -> dict:
    """Prove that an extracted URL yields media before it can be cached."""
    if stream_info.get("protocol") not in {None, "", "http", "https"}:
        raise RuntimeError("Requested format is not available: direct audio required")
    headers = dict(stream_info.get("headers") or {})
    headers["range"] = "bytes=0-0"
    try:
        response = server.upstream_http.get(
            stream_info["url"],
            headers=headers,
            stream=True,
            timeout=(5, 20),
        )
    except Exception as exc:
        _mark_failure_stage(exc, "media_preflight")
        raise
    try:
        response.raise_for_status()
        first_chunk = next(
            (chunk for chunk in response.iter_content(chunk_size=1) if chunk),
            b"",
        )
        if not first_chunk:
            raise RuntimeError("stream_preflight_empty")
        content_type = str(response.headers.get("content-type") or "").casefold()
        if "mpegurl" in content_type or "dash+xml" in content_type:
            raise RuntimeError("Requested format is not available: playlist is not audio")
        if content_type.startswith(("text/", "application/json")):
            raise RuntimeError(f"stream_preflight_invalid_content_type:{content_type}")
        validated = dict(stream_info)
        validated["validated_at"] = time.time()
        validated["validation_video_id"] = video_id
        return validated
    except Exception as exc:
        _mark_failure_stage(exc, "media_preflight")
        raise
    finally:
        response.close()


def is_stream_info_cached(server: Any, video_id: str):
    now = time.time()
    with server.stream_info_lock:
        cached = server.stream_info_cache.get(video_id)
        return bool(cached and cached["expires_at"] > now)


def extract_total_length(headers):
    content_range = headers.get("content-range") or headers.get("Content-Range")
    if content_range and "/" in content_range:
        total = content_range.split("/")[-1].strip()
        if total.isdigit():
            return int(total)

    content_length = headers.get("content-length") or headers.get("Content-Length")
    if content_length and content_length.isdigit():
        return int(content_length)
    return None


def get_cached_stream_chunk(server: Any, video_id: str, min_bytes: int = 0):
    now = time.time()
    with server.stream_chunk_lock:
        cached = server.stream_chunk_cache.get(video_id)
        if cached and cached["expires_at"] > now:
            payload = cached["payload"]
            if len(payload.get("bytes") or b"") >= min_bytes:
                return payload
            return None
        if cached:
            server.stream_chunk_cache.pop(video_id, None)
    return None


def store_stream_chunk(server: Any, video_id: str, payload):
    with server.stream_chunk_lock:
        server.stream_chunk_cache[video_id] = {
            "payload": payload,
            "expires_at": time.time() + server.STREAM_CHUNK_TTL_SECONDS,
        }


def chunk_target_bytes(server: Any, position: int, active_queue: bool):
    if active_queue:
        if position <= 0:
            return 262144
        if position == 1:
            return 196608
        if position == 2:
            return 131072
        return min(server.STREAM_WARM_CHUNK_BYTES, 131072)
    if position <= 0:
        return 196608
    if position == 1:
        return 131072
    return min(server.STREAM_WARM_CHUNK_BYTES, 131072)


def parse_byte_range(range_header: Optional[str], total_length: Optional[int]):
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


def record_prepare_metric(server: Any, video_id: str, metrics: dict):
    with server.prepare_metrics_lock:
        server.prepare_metrics.append({
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


def summarize_prepare_metrics(server: Any):
    with server.prepare_metrics_lock:
        items = list(server.prepare_metrics)

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
            sum(1 for item in items if item["resolve_cache_hit"]) / count,
            3,
        ),
        "chunk_cache_hit_rate": round(
            sum(1 for item in items if item["chunk_cache_hit"]) / count,
            3,
        ),
    }


def warm_initial_stream_chunk(server: Any, video_id: str, stream_info: dict, target_bytes: int):
    # The caller already assigns an intent-weighted prefix size. Clamping every
    # request back to the old global 768 KiB target erased that optimization and
    # made a one-track tap download far more data than playback needs to start.
    target_bytes = max(int(target_bytes or 0), 64 * 1024)
    while True:
        cached = get_cached_stream_chunk(server, video_id, min_bytes=target_bytes)
        if cached is not None and cached.get("compatibility") == stream_info.get("compatibility"):
            return cached

        with server.stream_chunk_inflight_lock:
            pending = server.stream_chunk_inflight.get(video_id)
            if pending is None:
                pending = Future()
                server.stream_chunk_inflight[video_id] = pending
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
            existing = get_cached_stream_chunk(server, video_id)
            if existing and existing.get("compatibility") != stream_info.get("compatibility"):
                existing = None
            existing_bytes = existing.get("bytes") if existing else b""
            total_length = existing.get("total_length") if existing else None
            if total_length is not None and len(existing_bytes) >= total_length:
                pending.set_result(existing)
                return existing

            headers = dict(stream_info["headers"])
            start_offset = len(existing_bytes)
            headers["range"] = f"bytes={start_offset}-{max(target_bytes - 1, start_offset)}"
            req = server.upstream_http.get(
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
                    "total_length": extract_total_length(req.headers) or total_length,
                    "compatibility": stream_info.get("compatibility"),
                }
                store_stream_chunk(server, video_id, payload)
                pending.set_result(payload)
                return payload
            finally:
                req.close()
        except Exception as exc:
            pending.set_exception(exc)
            raise
        finally:
            with server.stream_chunk_inflight_lock:
                if server.stream_chunk_inflight.get(video_id) is pending:
                    server.stream_chunk_inflight.pop(video_id, None)


def _defer_initial_stream_chunk(
    server: Any,
    video_id: str,
    stream_info: dict,
    target_chunk_bytes: int,
) -> None:
    try:
        server.stream_warm_executor.submit(
            warm_initial_stream_chunk,
            server,
            video_id,
            stream_info,
            target_chunk_bytes,
        )
    except Exception:
        # Playback can still stream directly from upstream if background warm
        # scheduling fails. The prepare response should stay fast and usable.
        return


def prepare_stream_track(
    server: Any,
    video_id: str,
    target_chunk_bytes: int,
    active_queue: bool,
    *,
    defer_chunk: bool = False,
):
    total_start = time.perf_counter()
    stream_cache = get_stream_cache(server)
    stream_cache_head_start = time.perf_counter()
    stream_cache_meta = stream_cache.head(video_id)
    stream_cache_head_ms = int((time.perf_counter() - stream_cache_head_start) * 1000)
    if stream_cache_meta:
        content_length = int(stream_cache_meta.get("content_length") or 0)
        metrics = {
            "prepared": True,
            "playback_path": "",
            "playback_url": "",
            "source_kind": f"{stream_cache.backend_name}_proxy",
            "headers": {},
            "mime_type": stream_cache_meta.get("content_type") or "audio/mp4",
            "content_length": content_length,
            "range_supported": True,
            "expires_at": time.time() + (60 * 60),
            "r2_cache_hit": bool(stream_cache.backend_name == "r2"),
            "resolve_cache_hit": False,
            "chunk_cache_hit": True,
            "chunk_deferred": False,
            "resolve_ms": 0,
            "chunk_ms": 0,
            "stream_cache_head_ms": stream_cache_head_ms,
            "target_chunk_bytes": target_chunk_bytes,
            "cached_prefix_bytes": min(content_length, target_chunk_bytes) if content_length else target_chunk_bytes,
            "duration": 0,
            "active_queue": active_queue,
            "server_ms": int((time.perf_counter() - total_start) * 1000),
        }
        record_prepare_metric(server, video_id, metrics)
        return metrics
    resolve_start = time.perf_counter()
    resolve_cache_hit = is_stream_info_cached(server, video_id)
    stream_info = get_stream_info(server, video_id, background=not active_queue)
    resolve_ms = int((time.perf_counter() - resolve_start) * 1000)

    chunk_cache_hit = get_cached_stream_chunk(
        server,
        video_id,
        min_bytes=target_chunk_bytes,
    ) is not None
    chunk_start = time.perf_counter()
    chunk_payload = None
    if defer_chunk and not chunk_cache_hit:
        _defer_initial_stream_chunk(server, video_id, stream_info, target_chunk_bytes)
        chunk_ms = 0
    else:
        try:
            chunk_payload = warm_initial_stream_chunk(
                server,
                video_id,
                stream_info,
                target_chunk_bytes,
            )
        except Exception as exc:
            if not should_refresh_stream_info(exc):
                raise
            stream_info = refresh_stream_info(server, video_id, background=not active_queue)
            chunk_payload = warm_initial_stream_chunk(
                server,
                video_id,
                stream_info,
                target_chunk_bytes,
            )
        chunk_ms = int((time.perf_counter() - chunk_start) * 1000)

    playback_path = ""
    playback_url = stream_info.get("url") or ""
    source_kind = "direct_resolved"
    playback_headers = dict(stream_info.get("headers") or {})
    content_length = int(stream_info.get("filesize") or stream_info.get("filesize_approx") or 0)
    mime_type = stream_info.get("mime_type") or "audio/mp4"

    stream_info_expiry = float(
        stream_info.get("expires_at") or (time.time() + (20 * 60))
    )
    metrics = {
        "prepared": True,
        "playback_path": playback_path,
        "playback_url": playback_url,
        "source_kind": source_kind,
        "headers": playback_headers,
        "mime_type": mime_type,
        "content_length": content_length,
        "range_supported": True,
        "expires_at": min(stream_info_expiry, time.time() + (20 * 60)),
        "r2_cache_hit": bool(stream_cache_meta and stream_cache.backend_name == "r2"),
        "resolve_cache_hit": resolve_cache_hit,
        "chunk_cache_hit": chunk_cache_hit,
        "chunk_deferred": bool(defer_chunk and not chunk_cache_hit),
        "resolve_ms": resolve_ms,
        "chunk_ms": chunk_ms,
        "stream_cache_head_ms": stream_cache_head_ms,
        "target_chunk_bytes": target_chunk_bytes,
        "cached_prefix_bytes": len((chunk_payload or {}).get("bytes") or b""),
        "duration": stream_info.get("duration") or 0,
        "active_queue": active_queue,
        "server_ms": int((time.perf_counter() - total_start) * 1000),
    }
    record_prepare_metric(server, video_id, metrics)
    return metrics


def prepare_stream_track_safely(server: Any, video_id: str, target_chunk_bytes: int, active_queue: bool):
    try:
        return prepare_stream_track(server, video_id, target_chunk_bytes, active_queue)
    except Exception as exc:
        _store_stream_failure(server, video_id, exc)
        return None


def classify_stream_failure(exc: Exception):
    message = str(exc or "").strip()
    lowered = message.lower()
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if "video unavailable" in lowered or "this video is not available" in lowered:
        return {
            "code": "video_unavailable",
            "message": message or "Video unavailable",
            "status_code": 410,
        }
    if "requested format is not available" in lowered:
        return {
            "code": "format_unavailable",
            "message": message or "Requested format is not available",
            "status_code": 410,
        }
    if (
        status_code in {401, 403, 429}
        or "sign in to confirm" in lowered
        or "not a bot" in lowered
        or "source_blocked" in lowered
    ):
        return {
            "code": "source_blocked",
            "message": message or "Upstream source requires verification",
            "status_code": 502,
        }
    return {
        "code": "stream_failed",
        "message": message or exc.__class__.__name__,
        "status_code": 500,
    }


def prepare_streams_with_failures(
    server: Any,
    video_ids: List[str],
    limit: int = 18,
    current_video_id: Optional[str] = None,
    active_queue: bool = False,
    defer_all_chunks: bool = False,
):
    prepared = {}
    failed = {}
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
        return prepared, failed

    if current_video_id and current_video_id in deduped_ids:
        deduped_ids.remove(current_video_id)
        deduped_ids.insert(0, current_video_id)

    targets = {
        video_id: chunk_target_bytes(server, index, active_queue)
        for index, video_id in enumerate(deduped_ids)
    }

    max_workers = min(server.PREPARE_SESSION_WORKERS, len(deduped_ids))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                prepare_stream_track,
                server,
                video_id,
                targets[video_id],
                active_queue,
                defer_chunk=defer_all_chunks,
            ): video_id
            for video_id in deduped_ids
        }
        for future, video_id in future_map.items():
            try:
                prepared[video_id] = future.result(timeout=25)
            except Exception as exc:
                failed[video_id] = classify_stream_failure(exc)
    return prepared, failed


def prepare_streams(
    server: Any,
    video_ids: List[str],
    limit: int = 18,
    current_video_id: Optional[str] = None,
    active_queue: bool = False,
):
    prepared, _ = prepare_streams_with_failures(
        server,
        video_ids,
        limit=limit,
        current_video_id=current_video_id,
        active_queue=active_queue,
    )
    return prepared


def selected_stream_info(server: Any, video_id: str, session: str = ''):
    if not hasattr(server, 'stream_info_lock'):
        return None
    with server.stream_info_lock:
        entry = server.stream_info_cache.get(f'selected:{session}:{video_id}')
        if entry and entry['expires_at'] > time.time():
            return entry['payload']
        if entry and session:
            raise RuntimeError('Playback source expired; start playback again')
    return None


def get_stream_info(server: Any, video_id: str, *, background=False, session=''):
    if not os.environ.get('AURALIS_BROWSER_AUDIO_URL', '').strip():
        return _resolve_stream_info(server, video_id, background=background)
    selected = selected_stream_info(server, video_id, session)
    if selected is not None:
        _cache_capture(server, video_id, selected)
        return selected
    key = f'selected:{session}:{video_id}'
    with server.stream_info_lock:
        pending = server.stream_info_inflight.get(key)
        owner = pending is None
        if owner:
            pending = Future()
            server.stream_info_inflight[key] = pending
    if not owner:
        return pending.result(timeout=85)
    try:
        payload = selected_stream_info(server, video_id, session)
        if payload is None and session:
            payload = selected_stream_info(server, video_id)
            if payload is not None and payload.get('compatibility'):
                try:
                    payload = validate_stream_info(server, video_id, payload)
                except Exception:
                    with server.stream_info_lock:
                        server.stream_info_cache.pop(f'selected::{video_id}', None)
                    payload = None
            if payload is not None:
                print(f'[EBB:stream] video={video_id} event=prepared_source_reused')
        if payload is None:
            payload = _resolve_stream_info(server, video_id, background=background,
                                           priority=0 if session else (2 if background else 1))
        payload = {**payload, 'pinned': bool(session)}
        if session and payload.get('compatibility'):
            parts = urllib.parse.urlsplit(payload['url'])
            query = dict(urllib.parse.parse_qsl(parts.query))
            query.pop('background', None)
            query.pop('priority', None)
            payload['url'] = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
        with server.stream_info_lock:
            keys = [k for k in server.stream_info_cache if k.startswith('selected:')]
            for old in keys[:-255]:
                server.stream_info_cache.pop(old, None)
            server.stream_info_cache[key] = {
                'payload': payload,
                'expires_at': float(payload.get('expires_at') or time.time()+1200),
            }
        pending.set_result(payload)
        _cache_capture(server, video_id, payload)
        return payload
    except Exception as exc:
        pending.set_exception(exc)
        raise
    finally:
        with server.stream_info_lock:
            server.stream_info_inflight.pop(key, None)


def _resolve_stream_info(server: Any, video_id: str, *, background=False, priority=0):
    try:
        if not background and os.environ.get('AURALIS_BROWSER_AUDIO_URL', '').strip():
            from .browser_compat import race_primary, resolve_browser_audio
            return race_primary(lambda: _get_primary_stream_info(server, video_id),
                                lambda: resolve_browser_audio(video_id, 'slow_primary', priority=priority))
        return _get_primary_stream_info(server, video_id)
    except Exception as exc:
        from .browser_compat import resolve_browser_audio
        failure = _get_stream_failure(server, video_id) or classify_stream_failure(exc)
        fallback = (resolve_browser_audio(video_id, failure.get('code'), background=True)
                    if background else resolve_browser_audio(video_id, failure.get('code'), priority=priority))
        if fallback is not None:
            return fallback
        raise


def _get_primary_stream_info(server: Any, video_id: str):
    now = time.time()
    cached_failure = _get_stream_failure(server, video_id)
    if cached_failure is not None:
        raise RuntimeError(cached_failure.get("message") or cached_failure.get("code") or "stream_failed")
    with server.stream_info_lock:
        cached = server.stream_info_cache.get(video_id)
        if cached and cached["expires_at"] > now:
            return cached["payload"]

        pending = server.stream_info_inflight.get(video_id)
        if pending is None:
            pending = Future()
            server.stream_info_inflight[video_id] = pending
            should_extract = True
        else:
            should_extract = False

    if not should_extract:
        return pending.result(timeout=25)

    try:
        persisted = _load_persisted_stream_info(server, video_id)
        if persisted is not None:
            payload, expires_at = persisted
            try:
                payload = validate_stream_info(server, video_id, payload)
            except Exception:
                _delete_persisted_stream_info(server, video_id)
                payload = validate_stream_info(
                    server,
                    video_id,
                    extract_stream_info(server, video_id),
                )
                expires_at, should_persist = _resolved_stream_expiry(server, payload)
                payload["expires_at"] = expires_at
                if should_persist:
                    _persist_stream_info(server, video_id, payload, expires_at)
        else:
            payload = validate_stream_info(
                server,
                video_id,
                extract_stream_info(server, video_id),
            )
            expires_at, should_persist = _resolved_stream_expiry(server, payload)
            payload = dict(payload)
            payload["expires_at"] = expires_at
            if should_persist:
                _persist_stream_info(server, video_id, payload, expires_at)
        _clear_stream_failure(server, video_id)
        with server.stream_info_lock:
            server.stream_info_cache[video_id] = {
                "payload": payload,
                "expires_at": expires_at,
            }
        pending.set_result(payload)
        return payload
    except Exception as exc:
        _store_stream_failure(server, video_id, exc)
        pending.set_exception(exc)
        raise
    finally:
        with server.stream_info_lock:
            if server.stream_info_inflight.get(video_id) is pending:
                server.stream_info_inflight.pop(video_id, None)


def refresh_stream_info(server: Any, video_id: str, *, background=False):
    with server.stream_info_lock:
        server.stream_info_cache.pop(video_id, None)
        server.stream_info_cache.pop(f'selected::{video_id}', None)
    _delete_persisted_stream_info(server, video_id)
    return get_stream_info(server, video_id, background=True) if background else get_stream_info(server, video_id)


def should_refresh_stream_info(exc: Exception):
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code in {401, 403, 404, 410, 416}


def open_upstream_stream(
    server: Any,
    video_id: str,
    stream_info: dict,
    start: int = 0,
    end: Optional[int] = None,
):
    """Open and validate upstream before FastAPI commits response headers."""
    current_stream_info = stream_info
    refreshed = False
    while True:
        headers = dict(current_stream_info.get("headers") or {})
        if start > 0 or end is not None or current_stream_info.get('pinned') or current_stream_info.get('compatibility'):
            request_end = end
            if current_stream_info.get('pinned') or current_stream_info.get('compatibility'):
                request_end = min(end if end is not None else start+1048575, start+1048575)
            headers["range"] = (
                f"bytes={start}-" if request_end is None else f"bytes={start}-{request_end}"
            )
        response = server.upstream_http.get(
            current_stream_info["url"],
            headers=headers,
            stream=True,
            timeout=(8, 90),
        )
        try:
            response.raise_for_status()
            return response, current_stream_info
        except Exception as exc:
            response.close()
            if current_stream_info.get('pinned') or refreshed or not should_refresh_stream_info(exc):
                raise
            refreshed = True
            current_stream_info = refresh_stream_info(server, video_id)


def iter_upstream_stream(
    server: Any,
    video_id: str,
    stream_info: dict,
    start: int = 0,
    end: Optional[int] = None,
    initial_response: Any = None,
):
    if stream_info.get('pinned') or stream_info.get('compatibility'):
        offset = start
        response = initial_response
        total = None
        failures = 0
        while True:
            try:
                if response is None:
                    response, _ = open_upstream_stream(server, video_id, stream_info, offset, end)
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('content-range', ''))
                if response.status_code != 206 or not match or int(match[1]) != offset:
                    raise ValueError('Upstream did not honor the byte range')
                response_end, length = int(match[2]), int(match[3])
                if total is not None and total != length:
                    raise ValueError('Audio representation length changed')
                total = length
                target = min(end if end is not None else total-1, total-1)
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    if offset+len(chunk) > min(response_end, target)+1:
                        raise ValueError('Upstream exceeded the byte range')
                    offset += len(chunk)
                    yield chunk
                if offset != response_end+1:
                    raise IOError('Incomplete audio range')
                if offset > target:
                    return
            except ValueError:
                raise
            except Exception:
                failures += 1
                if failures > 2:
                    print(f'[EBB:stream] video={video_id} event=retry_exhausted offset={offset} attempts={failures}')
                    raise
                print(f'[EBB:stream] video={video_id} event=same_source_retry offset={offset} attempt={failures}')
                time.sleep(.15)
            finally:
                if response is not None:
                    response.close()
                    response = None
        return
    current_start = max(start, 0)
    current_stream_info = stream_info
    attempts = 0
    refreshed = False
    emitted_bytes = 0

    while True:
        req = initial_response
        initial_response = None
        try:
            if req is None:
                req, current_stream_info = open_upstream_stream(
                    server,
                    video_id,
                    current_stream_info,
                    start=current_start,
                    end=end,
                )

            for chunk in req.iter_content(chunk_size=1024 * 64):
                if not chunk:
                    continue
                yield chunk
                current_start += len(chunk)
                emitted_bytes += len(chunk)
                if end is not None and current_start > end:
                    return
            return
        except Exception as exc:
            if current_stream_info.get('compatibility') or current_stream_info.get('pinned'):
                # A partial capture must not become a successful EOF or be
                # resumed from a different representation and cached as full.
                raise
            if attempts >= 2:
                if emitted_bytes:
                    print(
                        "[EBB:stream] "
                        f"video={video_id} event=upstream_ended_after_start "
                        f"bytes={emitted_bytes} error={exc.__class__.__name__}"
                    )
                    return
                raise
            attempts += 1
            if should_refresh_stream_info(exc) and not refreshed:
                refreshed = True
                try:
                    current_stream_info = refresh_stream_info(server, video_id)
                except Exception:
                    raise exc
            time.sleep(0.15)
        finally:
            if req is not None:
                req.close()
