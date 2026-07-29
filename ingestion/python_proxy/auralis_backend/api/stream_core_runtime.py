from __future__ import annotations

import json
import time
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, List, Optional

import yt_dlp

from .stream_cache import get_stream_cache

_STREAM_RESOLUTION_NAMESPACE = "playback_stream_resolution"
_STREAM_RESOLUTION_MODEL = "youtube"
_STREAM_RESOLUTION_EXPIRY_MARGIN_SECONDS = 90


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


def _ytdlp_opts(server: Any) -> dict:
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
    }
    cookies_path = str(getattr(server, "AURALIS_YTDLP_COOKIES_PATH", "") or "").strip()
    if cookies_path:
        opts["cookiefile"] = cookies_path
    po_token = str(getattr(server, "AURALIS_YTDLP_PO_TOKEN", "") or "").strip()
    if po_token:
        opts.setdefault("extractor_args", {}).setdefault("youtube", {})["po_token"] = [po_token]
    return opts


def extract_stream_info(server: Any, video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = _ytdlp_opts(server)
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
        if cached is not None:
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
    cached_failure = _get_stream_failure(server, video_id)
    if cached_failure is not None:
        raise RuntimeError(cached_failure.get("message") or cached_failure.get("code") or "stream_failed")
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
    stream_info = get_stream_info(server, video_id)
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
            stream_info = refresh_stream_info(server, video_id)
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
    if "sign in to confirm" in lowered or "not a bot" in lowered or "source_blocked" in lowered:
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


def get_stream_info(server: Any, video_id: str):
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
        else:
            payload = extract_stream_info(server, video_id)
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


def refresh_stream_info(server: Any, video_id: str):
    with server.stream_info_lock:
        server.stream_info_cache.pop(video_id, None)
    _delete_persisted_stream_info(server, video_id)
    return get_stream_info(server, video_id)


def should_refresh_stream_info(exc: Exception):
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code in {401, 403, 404, 410, 416}


def iter_upstream_stream(
    server: Any,
    video_id: str,
    stream_info: dict,
    start: int = 0,
    end: Optional[int] = None,
):
    current_start = max(start, 0)
    current_stream_info = stream_info
    attempts = 0
    refreshed = False

    while True:
        req = None
        try:
            headers = dict(current_stream_info["headers"])
            if current_start > 0 or end is not None:
                if end is None:
                    headers["range"] = f"bytes={current_start}-"
                else:
                    headers["range"] = f"bytes={current_start}-{end}"

            req = server.upstream_http.get(
                current_stream_info["url"],
                headers=headers,
                stream=True,
                timeout=(8, 90),
            )
            req.raise_for_status()

            for chunk in req.iter_content(chunk_size=1024 * 64):
                if not chunk:
                    continue
                yield chunk
                current_start += len(chunk)
                if end is not None and current_start > end:
                    return
            return
        except Exception as exc:
            if attempts >= 2:
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
