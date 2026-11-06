"""Playback preparation and delivery; low-level owners are re-exported for callers."""
from __future__ import annotations

import time
import re
import uuid
from threading import (BoundedSemaphore)
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, List, Optional


from .stream_cache import (get_stream_cache, iter_and_store_full_stream, cached_resource_version, cached_variant, current_audio_index, cached_audio_duration, aac_playback_enabled)


from .stream_sources import (
    start_playback_tools,
    stop_playback_tools,
    _defer_stream_store,
    PlaybackSourceExpired,
    _resolved_stream_expiry,
    _load_persisted_stream_info,
    _persist_stream_info,
    _delete_persisted_stream_info,
    _delete_stream_info_before,
    CachedStreamFailure,
    explicit_source_retry,
    _stream_failure_payload,
    _get_stream_failure,
    _store_stream_failure,
    _clear_stream_failure,
    _QuietYtDlpLogger,
    _ytdlp_opts,
    _mark_failure_stage,
    stream_failure_diagnostics,
    record_resolution_metrics,
    _new_ytdlp,
    _ytdlp_extractor,
    extract_stream_info,
    validate_stream_info,
    is_stream_info_cached,
    classify_stream_failure,
    selected_stream_info,
    _remember_selected_stream,
    get_stream_info,
    _resolve_stream_info,
    _get_primary_stream_info,
    refresh_stream_info,
    should_refresh_stream_info,
    BACKGROUND_EXTRACTION_CAPACITY,
    _direct_extraction_slots,
    _direct_background_slots,
    _background_acquisition,
    _explicit_source_retry,
    _direct_client_override,
    playback_trace_context,
    _startup_tools_lock,
    _startup_tools_thread,
    _startup_tools_stop,
    _STREAM_RESOLUTION_NAMESPACE,
    _STREAM_RESOLUTION_MODEL,
    _STREAM_RESOLUTION_EXPIRY_MARGIN_SECONDS,
)
from .stream_buffers import (
    _close_initial_read,
    _expire_initial_read,
    _keep_initial_read,
    _take_initial_read,
    extract_total_length,
    get_cached_stream_chunk,
    store_stream_chunk,
    cached_object_key,
    warm_cached_object_prefix,
    prepared_cached_object,
    stream_representation_key,
    stream_chunk_matches,
    parse_byte_range,
    warm_initial_stream_chunk,
    _initial_reads,
    _initial_reads_lock,
)
from .stream_cached_audio import (
    validate_cached_track,
    _cache_proof_key,
    _needs_cached_repair,
    _validate_cached_track,
    validate_cached_aac_track,
    preferred_cached_audio,
    remember_aac_demand,
    schedule_cached_audio_repair,
    _cached_repair_slot,
)

_capture_cache_slots = BoundedSemaphore(2)
_preparation_slots = BoundedSemaphore(8)
PREPARATION_RESPONSE_SECONDS = 85

def iter_initial_read(server, video_id, info, chunk, start, end=None):
    """Claim one already validated connection, or continue the exact pinned source."""
    entry = _take_initial_read(server, video_id, info, chunk, start)
    offset = start
    target = min(end if end is not None else chunk['total_length']-1, chunk['total_length']-1)
    if entry is not None:
        position = entry['offset']
        try:
            try:
                for block in entry['chunks']:
                    if not block:
                        continue
                    next_position = position+len(block)
                    if next_position > entry['last']+1:
                        raise ValueError('Initial audio read exceeded its range')
                    left, right = max(offset-position, 0), min(target-position+1, len(block))
                    position = next_position
                    if right > left:
                        offset += right-left
                        yield block[left:right]
                    if offset > target:
                        return
                if position != entry['last']+1:
                    raise IOError('Initial audio read ended early')
            except ValueError:
                raise
            except Exception:
                # Recovery stays on the same URI/representation after emission.
                pass
        finally:
            _close_initial_read(entry)
    if offset <= target:
        yield from iter_upstream_stream(server, video_id, {**info, 'pinned': True},
            start=offset, end=end, expected_total_length=chunk['total_length'],
            expected_content_type=chunk['content_type'], expected_etag=chunk.get('etag') or '')


def cache_prepared_track(server, video_id, *, max_bytes=None):
    cache = get_stream_cache(server)
    if not cache.enabled:
        return False
    head = cache.head(video_id)
    if head and int(head.get('content_length') or 0) <= 0:
        return False
    if head and current_audio_index(head):
        schedule_cached_audio_repair(server, video_id, head)
        return True
    key = f'cache:{video_id}'
    with server.stream_info_lock:
        pending = server.stream_info_inflight.get(key)
        owner = pending is None
        if owner:
            pending = Future()
            server.stream_info_inflight[key] = pending
    if not owner:
        try:
            return bool(pending.result(timeout=75))
        except Exception:
            return False
    acquired = _capture_cache_slots.acquire(blocking=False)
    stored = False
    try:
        if not acquired:
            return False
        if head:
            stored = validate_cached_track(server, video_id)
            return stored
        info = selected_stream_info(server, video_id)
        if not info:
            return False
        for _ in iter_and_store_full_stream(server, video_id,
                iter_upstream_stream(server, video_id, {**info, 'pinned': True}),
                content_type=info.get('mime_type', 'audio/mp4'),
                capture_duration=info.get('duration') or None,
                max_bytes_override=max_bytes, independent=True):
            pass
        stored = bool(cache.head(video_id))
        return stored
    except Exception as exc:
        print(f'[EBB:stream] video={video_id} event=queue_cache_failed error={type(exc).__name__}')
        return False
    finally:
        if acquired:
            _capture_cache_slots.release()
        pending.set_result(stored)
        with server.stream_info_lock:
            if server.stream_info_inflight.get(key) is pending:
                server.stream_info_inflight.pop(key)


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


def cached_stream_descriptor(cache, video_id, metadata):
    """Describe one exact cached file without downloading or resolving audio."""
    path = ''
    if cache.backend_name == 'r2' and isinstance(metadata.get('key'), str):
        variant = cached_variant(metadata)
        path = f'/playback/stream/{video_id}?resolve=1&source_id={video_id}&cache_variant={variant}'
        version = cached_resource_version(metadata)
        if version:
            path += f'&cache_version={version}'
    delivery = cache.direct_delivery(video_id, metadata) if callable(getattr(cache, 'direct_delivery', None)) else {}
    return {
        'prepared': True,
        'playback_path': path,
        'playback_url': '',
        'source_kind': f'{cache.backend_name}_proxy',
        'cache_tier': 'r2_full' if cache.backend_name == 'r2' else 'local_full',
        'headers': {},
        'mime_type': metadata.get('content_type') or 'audio/mp4',
        'content_length': int(metadata.get('content_length') or 0),
        'range_supported': True,
        # Immutable versions remain playable while a preferred copy is built.
        # Metadata refresh is separate from file validity, including while the
        # browser is hidden for longer than the current song.
        'expires_at': time.time() + (12 * 60 * 60 if current_audio_index(metadata) else 45),
        'duration': cached_audio_duration(metadata),
        'r2_cache_hit': cache.backend_name == 'r2',
        **(delivery if isinstance(delivery, dict) else {}),
    }


def prepare_stream_track(
    server: Any,
    video_id: str,
    target_chunk_bytes: int,
    active_queue: bool,
    *,
    defer_chunk: bool = False,
    preferred_cached_variant: str = '',
):
    total_start = time.perf_counter()
    stream_cache = get_stream_cache(server)
    stream_cache_head_start = time.perf_counter()
    selected_prefix = (prepared_cached_object(server, video_id, preferred_cached_variant)
                       if preferred_cached_variant in {'aac-v1', 'opus-v1'}
                       and (preferred_cached_variant != 'aac-v1' or aac_playback_enabled()) else None)
    stream_cache_meta = (selected_prefix or prepared_cached_object(server, video_id)
                         or stream_cache.head(video_id))
    if stream_cache_meta and not selected_prefix and preferred_cached_variant in {'aac-v1', 'opus-v1'}:
        stream_cache_meta = preferred_cached_audio(
            server, video_id, preferred_cached_variant, schedule=active_queue,
            fallback=stream_cache_meta) or stream_cache_meta
    stream_cache_head_ms = int((time.perf_counter() - stream_cache_head_start) * 1000)
    if stream_cache_meta:
        descriptor = cached_stream_descriptor(stream_cache, video_id, stream_cache_meta)
        if stream_cache.backend_name == 'r2':
            schedule_cached_audio_repair(server, video_id, stream_cache_meta)
        prefix = None
        prefix_started = time.perf_counter()
        # Native/older clients still use playback_path even when an optional
        # delivery capability is supplied. Preserve their prepared relay prefix.
        # New web clients can reuse metadata-only capabilities without this call.
        if stream_cache.backend_name == 'r2':
            try:
                prefix = warm_cached_object_prefix(server, video_id, stream_cache, stream_cache_meta)
            except Exception:
                pass  # A prefix miss is not proof that startup bytes exist.
        metrics = {
            **descriptor,
            "resolve_cache_hit": False,
            "chunk_cache_hit": prefix is not None,
            "chunk_deferred": prefix is None,
            "resolve_ms": 0,
            "chunk_ms": int((time.perf_counter()-prefix_started)*1000),
            "stream_cache_head_ms": stream_cache_head_ms,
            "target_chunk_bytes": target_chunk_bytes,
            "cached_prefix_bytes": len(prefix['bytes']) if prefix else 0,
            "startup_ready_until": min(time.time() + server.STREAM_CHUNK_TTL_SECONDS,
                prefix.get('object_proven_until', 0)) if prefix and prefix.get('startup_ready') else 0,
            "duration": cached_audio_duration(stream_cache_meta),
            "active_queue": active_queue,
            "server_ms": int((time.perf_counter() - total_start) * 1000),
        }
        record_prepare_metric(server, video_id, metrics)
        return metrics
    resolve_start = time.perf_counter()
    resolve_cache_hit = is_stream_info_cached(server, video_id)
    stream_info = get_stream_info(server, video_id, background=not active_queue)
    resolve_ms = int((time.perf_counter() - resolve_start) * 1000)

    chunk_cache_hit = stream_chunk_matches(get_cached_stream_chunk(
        server,
        video_id,
        min_bytes=target_chunk_bytes,
    ), stream_info)
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

    # Preserve the validated representation, including its type, across every
    # browser probe/range. A later completed cache must not replace these bytes.
    session = uuid.uuid4().hex
    stream_info_expiry = min(float(stream_info.get('expires_at') or time.time() + 1200),
                             time.time() + 1200)
    _remember_selected_stream(server, video_id, session, {**stream_info, 'pinned': True},
                              stream_info_expiry)
    playback_path = (f'/playback/stream/{video_id}?playback_session={session}'
                     f'&stream_version={stream_representation_key(stream_info)}')
    playback_url = stream_info.get("url") or ""
    source_kind = "direct_resolved"
    playback_headers = dict(stream_info.get("headers") or {})
    content_length = int(stream_info.get("filesize") or stream_info.get("filesize_approx") or 0)
    mime_type = stream_info.get("mime_type") or "audio/mp4"

    metrics = {
        "prepared": True,
        "playback_path": playback_path,
        "playback_url": playback_url,
        "source_kind": source_kind,
        "cache_tier": "prefix" if chunk_payload or chunk_cache_hit else "resolved" if resolve_cache_hit else "cold",
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


def prepare_streams_with_failures(
    server: Any,
    video_ids: List[str],
    limit: int = 18,
    current_video_id: Optional[str] = None,
    active_queue: bool = False,
    defer_all_chunks: bool = False,
    preferred_cached_variant: str = '',
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
    deadline = time.monotonic() + PREPARATION_RESPONSE_SECONDS
    executor = ThreadPoolExecutor(max_workers=max_workers)
    future_map = {}
    try:
        for video_id in deduped_ids:
            if not _preparation_slots.acquire(blocking=False):
                failed[video_id] = classify_stream_failure(RuntimeError('audio_resolver_busy'))
                continue
            try:
                future = executor.submit(
                prepare_stream_track,
                server,
                video_id,
                targets[video_id],
                active_queue,
                defer_chunk=defer_all_chunks,
                **({'preferred_cached_variant': preferred_cached_variant}
                   if preferred_cached_variant else {}),
                )
            except BaseException:
                _preparation_slots.release()
                raise
            future.add_done_callback(lambda _: _preparation_slots.release())
            future_map[future] = video_id
        for future, video_id in future_map.items():
            try:
                prepared[video_id] = future.result(timeout=max(0, deadline-time.monotonic()))
            except Exception as exc:
                failed[video_id] = classify_stream_failure(exc)
    finally:
        # A response deadline must not join unfinished network work. Admission
        # remains bounded until running jobs really finish; queued jobs cancel.
        executor.shutdown(wait=False, cancel_futures=True)
    return prepared, failed


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
    expected_total_length: Optional[int] = None,
    expected_content_type: str = "",
    expected_etag: str = "",
):
    if stream_info.get('pinned') or stream_info.get('compatibility'):
        offset = start
        response = initial_response
        total = expected_total_length
        failures = 0
        while True:
            try:
                if response is None:
                    response, _ = open_upstream_stream(server, video_id, stream_info, offset, end)
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('content-range', ''))
                if response.status_code != 206 or not match or int(match[1]) != offset:
                    raise ValueError('Upstream did not honor the byte range')
                response_end, length = int(match[2]), int(match[3])
                if response_end < offset or response_end >= length:
                    raise ValueError('Invalid audio byte range')
                if total is not None and total != length:
                    raise ValueError('Audio representation length changed')
                content_type = str(response.headers.get('content-type') or '').split(';')[0]
                if expected_content_type and content_type != expected_content_type:
                    raise ValueError('Audio representation content type changed')
                if expected_etag and response.headers.get('etag') != expected_etag:
                    raise ValueError('Audio representation validator changed')
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
            if emitted_bytes:
                # An unpinned transfer cannot prove byte identity after a
                # reconnect. Preserve the error instead of reporting EOF or
                # stitching a refreshed representation into emitted audio.
                raise
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
