from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import uuid
import anyio
from anyio.lowlevel import RunVar
from concurrent.futures import ThreadPoolExecutor, wait
from threading import BoundedSemaphore, Lock
from typing import Any

import yt_dlp
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, Response, RedirectResponse

from .stream_cache import (
    cached_response_headers,
    cached_resource_version,
    cached_variant,
    current_audio_index,
    playback_content_type,
    get_stream_cache,
    iter_and_store_full_stream,
    should_cache_full_response,
)
from .stream_core_runtime import (
    _ytdlp_opts,
    classify_stream_failure,
    extract_total_length,
    get_cached_stream_chunk,
    cached_object_key,
    PlaybackSourceExpired,
    stream_chunk_matches,
    stream_representation_key,
    get_stream_info,
    selected_stream_info,
    iter_upstream_stream,
    iter_initial_read,
    open_upstream_stream,
    parse_byte_range,
    prepare_streams_with_failures,
    prepared_cached_object,
    explicit_source_retry,
    _stream_failure_payload,
    cached_stream_descriptor,
    playback_trace_context,
    record_resolution_metrics,
    schedule_cached_audio_repair,
)

# Short-lived, bounded operational traces addressed by an opaque browser token.
# No URLs, credentials, account IDs, database writes or unbounded event lists.
_delivery_lock = Lock()
_delivery_traces = {}
_DELIVERY_TRACE_TTL = 7200
_PROCESS_STARTED = time.monotonic()
_PROCESS_INSTANCE = uuid.uuid4().hex[:12]
_playback_limits = RunVar('neatie_playback_limits')
PLAYBACK_EXPOSE_HEADERS = ('Server-Timing', 'X-Neatie-Playback-Request',
    'X-Neatie-Edge-Request', 'X-Auralis-Stream-Cache', 'ETag', 'Content-Range', 'Accept-Ranges')


def _playback_limiter(lane):
    try:
        limits = _playback_limits.get()
    except LookupError:
        limits = {}
        _playback_limits.set(limits)
    if lane not in limits:
        limits[lane] = anyio.CapacityLimiter(4)
    return limits[lane]


async def dispatch_playback_stream(server, track_key, request):
    # Search/Home's default worker limiter cannot queue ready playback requests.
    try:
        return await anyio.to_thread.run_sync(playback_stream, server, track_key, request,
                                              limiter=_playback_limiter('request'))
    except HTTPException as exc:
        trace = playback_trace_context.get()
        if trace is not None and isinstance(exc.detail, dict):
            trace.setdefault('failureCode', exc.detail.get('code', 'stream_failed'))
        raise


def _next_audio_chunk(iterator):
    try:
        return True, next(iterator)
    except StopIteration:
        return False, None


async def _close_audio(resource, *, asynchronous=False, preserve_error=False):
    close = getattr(resource, 'aclose' if asynchronous else 'close', None)
    if close is None:
        return
    try:
        with anyio.CancelScope(shield=True):
            if asynchronous:
                await close()
            else:
                await anyio.to_thread.run_sync(close, limiter=_playback_limiter('bytes'))
    except Exception as exc:
        print(f'[EBB:playback-cleanup] error={type(exc).__name__}', flush=True)
        if not preserve_error:
            raise


async def _audio_chunks(chunks):
    iterator = iter(chunks)
    try:
        while True:
            available, block = await anyio.to_thread.run_sync(
                _next_audio_chunk, iterator, limiter=_playback_limiter('bytes'))
            if not available:
                return
            yield block
    finally:
        await _close_audio(iterator, preserve_error=sys.exc_info()[0] is not None)


class _AudioStreamingResponse(StreamingResponse):
    def __init__(self, *args, opened_source=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.opened_source = opened_source

    async def stream_response(self, send):
        try:
            await super().stream_response(send)
        finally:
            # A failed send leaves the iterator suspended at its last yield.
            try:
                await _close_audio(self.body_iterator, asynchronous=True,
                                   preserve_error=sys.exc_info()[0] is not None)
            finally:
                await _close_audio(self.opened_source, preserve_error=sys.exc_info()[0] is not None)


def _audio_response(chunks, *, opened_source=None, **kwargs):
    return _AudioStreamingResponse(_audio_chunks(chunks), opened_source=opened_source, **kwargs)


def playback_delivery_report(trace_id):
    if not re.fullmatch(r'[a-f0-9]{32}', trace_id):
        raise HTTPException(404, detail='Playback trace unavailable')
    with _delivery_lock:
        item = _delivery_traces.get(trace_id)
        if not item or time.monotonic() - item['at'] > _DELIVERY_TRACE_TTL:
            raise HTTPException(404, detail='Playback trace unavailable')
        return {'traceId': trace_id, 'requests': [dict(row) for row in item['requests']],
                'droppedRequests': item.get('droppedRequests', 0)}


def _delivery_stage(name, fn):
    started = time.perf_counter()
    try:
        return fn()
    finally:
        trace = playback_trace_context.get()
        if trace is not None:
            trace[name] = trace.get(name, 0) + round((time.perf_counter()-started)*1000, 1)


class PlaybackTraceMiddleware:
    """Observe the existing ASGI stream without buffering or changing workers."""
    def __init__(self, app, server=None):
        self.app = app
        self.server = server

    async def __call__(self, scope, receive, send):
        if self.server is not None and scope['type'] == 'http':
            from ..recommend.workload import note_demand, user_request
            if user_request(scope.get('path', ''), scope.get('method', '')):
                note_demand(self.server)
        if scope['type'] == 'http' and scope.get('path') == '/history_seed':
            began = time.perf_counter()
            scope['neatie_history_received'] = began
            async def history_send(message):
                if message['type'] == 'http.response.start':
                    headers = dict(message.get('headers', []))
                    timing = headers.get(b'server-timing',b'').decode('ascii','ignore')
                    timing += (', ' if timing else '') + f'api;dur={(time.perf_counter()-began)*1000:.2f}'
                    headers[b'server-timing'] = timing.encode()
                    message = {**message,'headers':list(headers.items())}
                await send(message)
            return await self.app(scope,receive,history_send)
        if scope['type'] != 'http' or not scope.get('path', '').startswith('/playback/stream/'):
            return await self.app(scope, receive, send)
        from anyio.to_thread import current_default_thread_limiter
        began = time.perf_counter()
        query = urllib.parse.parse_qs(scope.get('query_string', b'')[:8192].decode('utf-8', 'replace'))
        trace_id = query.get('diag', [''])[0]
        trace_id = trace_id if re.fullmatch(r'[a-f0-9]{32}', trace_id) else ''
        request_id = uuid.uuid4().hex
        trace_id = trace_id or request_id
        trace = {'requestId': request_id, 'at': round(time.time(), 3), 'result': 'pending',
                 'serverInstance': _PROCESS_INSTANCE,
                 'serverProcessAgeMs': round((time.monotonic()-_PROCESS_STARTED)*1000)}
        incoming = dict(scope.get('headers', []))
        edge = incoming.get(b'x-neatie-edge-request', b'').decode('ascii', 'ignore')
        if re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', edge):
            trace['edgeRequestId'] = edge
        # This is the API-to-proxy hop, never proof of receipt by the phone.
        trace['deliveryHop'] = 'api_to_proxy'
        trace['protocol'] = str(scope.get('http_version') or '')[:8]
        raw_range = incoming.get(b'range', b'').decode('ascii', 'ignore')
        if re.fullmatch(r'bytes=[0-9]*-[0-9]*', raw_range):
            trace['requestedRange'] = raw_range[:64]
        scope['neatie_playback_trace'] = trace
        scope['neatie_playback_started'] = began
        def workers(prefix):
            stats = current_default_thread_limiter().statistics()
            trace.update({prefix+'Borrowed': stats.borrowed_tokens,
                          prefix+'Total': stats.total_tokens, prefix+'Waiting': stats.tasks_waiting})
        workers('workersAtEntry')
        if trace_id:
            with _delivery_lock:
                for key in list(_delivery_traces):
                    if time.monotonic()-_delivery_traces[key]['at'] > _DELIVERY_TRACE_TTL:
                        _delivery_traces.pop(key, None)
                if trace_id not in _delivery_traces:
                    while len(_delivery_traces) >= 256:
                        _delivery_traces.pop(next(iter(_delivery_traces)))
                    _delivery_traces[trace_id] = {'at': time.monotonic(), 'requests': []}
                item = _delivery_traces[trace_id]
                item['requests'].append(trace)
                removed = max(0, len(item['requests']) - 32)
                item['droppedRequests'] = item.get('droppedRequests', 0) + removed
                del item['requests'][:-32]
        context_token = playback_trace_context.set(trace)
        trace['bytes'] = 0
        trace['sendWaitMs'] = 0
        trace['maxSendWaitMs'] = 0
        trace['bodyChunks'] = 0
        trace['disconnectObserved'] = False
        async def observed_receive():
            message = await receive()
            trace['lastReceiveType'] = message['type']
            if message['type'] == 'http.disconnect':
                trace.setdefault('disconnectMs', round((time.perf_counter()-began)*1000, 1))
                trace.setdefault('disconnectBeforeBodyComplete', trace['result'] != 'complete')
                trace['disconnectObserved'] = True
            return message
        async def observed_send(message):
            if message['type'] == 'http.response.start':
                trace['status'] = message['status']
                trace['headersMs'] = round((time.perf_counter()-began)*1000, 1)
                headers = dict(message.get('headers', []))
                length = headers.get(b'content-length', b'')
                content_range = headers.get(b'content-range', b'').decode('ascii', 'ignore')
                headers[b'x-neatie-playback-request'] = request_id.encode()
                if trace.get('edgeRequestId'):
                    headers[b'x-neatie-edge-request'] = trace['edgeRequestId'].encode()
                timing = headers.get(b'server-timing', b'').decode('ascii', 'ignore')
                timing += (', ' if timing else '') + f'route;dur={trace.get("routeWaitMs", 0)}'
                timing += (', ' if timing else '') + f'request;desc="{request_id}"'
                if length.isdigit():
                    timing += f', body;desc="{int(length)}"'
                if re.fullmatch(r'bytes [0-9]+-[0-9]+/[0-9]+', content_range):
                    timing += f', range;desc="{content_range}"'
                if trace.get('edgeRequestId'):
                    timing += f', edge;desc="{trace["edgeRequestId"]}"'
                if trace_id:
                    timing += f', trace;desc="{trace_id}"'
                headers[b'server-timing'] = timing.encode()
                headers[b'timing-allow-origin'] = b'*'
                message = {**message, 'headers': list(headers.items())}
                for header, field in [(b'content-type', 'mediaType'), (b'content-length', 'contentLength'),
                                      (b'content-range', 'contentRange'), (b'x-auralis-stream-cache', 'cacheTier')]:
                    if header in headers:
                        trace[field] = headers[header].decode('ascii', 'replace')[:120]
                if headers.get(b'content-length', b'').isdigit():
                    trace['bodyExpectedBytes'] = int(headers[b'content-length'])
            body = message.get('body', b'') if message['type'] == 'http.response.body' else b''
            if body and 'firstSendMs' not in trace:
                trace['firstSendMs'] = round((time.perf_counter()-began)*1000, 1)
                workers('workersAtFirstSend')
            send_at = time.perf_counter()
            try:
                await send(message)
            except BaseException as exc:
                trace['sendExceptionType'] = type(exc).__name__[:80]
                trace['failedSendBytes'] = len(body)
                trace['sendFailureMs'] = round((time.perf_counter()-began)*1000, 1)
                raise
            if body:
                elapsed = round((time.perf_counter()-send_at)*1000, 1)
                trace['sendWaitMs'] = round(trace['sendWaitMs']+elapsed, 1)
                trace['maxSendWaitMs'] = max(trace['maxSendWaitMs'], elapsed)
                trace['bytes'] += len(body)
                trace['bodyChunks'] += 1
                trace['lastBodyBytes'] = len(body)
                trace['lastSendMs'] = round((time.perf_counter()-began)*1000, 1)
                trace.setdefault('firstSendDoneMs', round((time.perf_counter()-began)*1000, 1))
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                trace['result'] = 'complete'
                trace['bodyCompleteMs'] = round((time.perf_counter()-began)*1000, 1)
        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException as exc:
            trace['result'] = 'cancelled' if type(exc).__name__ in {'CancelledError', 'ClientDisconnect'} else 'delivery_failed'
            trace['exceptionType'] = type(exc).__name__[:80]
            raise
        finally:
            if trace['result'] == 'pending':
                trace['result'] = 'cancelled'
            trace['totalMs'] = round((time.perf_counter()-began)*1000, 1)
            expected = trace.get('bodyExpectedBytes')
            trace['responseCompleted'] = trace['result'] == 'complete'
            if expected is not None and scope.get('method') != 'HEAD':
                trace['bodyRemainingBytes'] = max(0, expected - trace['bytes'])
                trace['allExpectedBytesSent'] = trace['bytes'] == expected
                trace['transferComplete'] = trace['bytes'] == expected and trace['result'] == 'complete'
            # A normal client disconnect may follow a fully completed response.
            # Keep that observation without calling the completed API-to-proxy
            # transfer a stream failure. Byte count alone cannot prove that the
            # final body message completed, nor that the phone received audio.
            completed = trace['responseCompleted'] and (
                scope.get('method') == 'HEAD' or expected is None
                or trace.get('allExpectedBytesSent') is True)
            trace['closureCategory'] = (
                'complete' if completed else
                'send_failed' if trace.get('sendExceptionType') else
                'downstream_disconnect_observed' if trace.get('disconnectObserved') else
                'body_incomplete' if trace.get('bodyRemainingBytes', 0) else
                'application_cancelled' if trace['result'] == 'cancelled' else
                trace['result'])
            playback_trace_context.reset(context_token)
            print('[EBB:delivery] ' + json.dumps({'traceId': trace_id, **trace}, separators=(',', ':')), flush=True)

# Metadata-only work shares the cache's HEAD coalescing, never extractor slots.
# Bound both running work and queued work when storage is slow/unavailable.
_cache_metadata_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix='cached-media')
_cache_metadata_slots = BoundedSemaphore(16)

def _catalog_entity_key(track_key: str) -> str:
    value = str(track_key or "").strip()
    if value.startswith("recording:"):
        recording_id = value.removeprefix("recording:")
        if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", recording_id):
            return f"musicbrainz:recording:{recording_id}"
        return recording_id
    return value


def playback_source_candidates_batch(
    server: Any,
    track_keys: list[str],
) -> dict[str, list[dict[str, str]]]:
    normalized_keys = list(
        dict.fromkeys(str(key or "").strip() for key in track_keys)
    )
    output: dict[str, list[dict[str, str]]] = {
        key: [] for key in normalized_keys if key
    }
    catalog_key_to_tracks: dict[str, list[str]] = {}
    for value in output:
        if value.startswith("audius:"):
            output[value] = [{
                "provider": "audius",
                "source_id": value.removeprefix("audius:"),
                "authority": "trusted_match",
            }]
            continue
        if not value.startswith("recording:") and not value.startswith(
            "musicbrainz:recording:"
        ):
            if (
                re.fullmatch(r"[A-Za-z0-9_-]{11}", value)
                and value.casefold() != "musicbrainz"
            ):
                output[value] = [
                    {
                        "provider": "youtube",
                        "source_id": value,
                        "authority": "legacy",
                    }
                ]
            continue
        catalog_key_to_tracks.setdefault(_catalog_entity_key(value), []).append(value)

    if not catalog_key_to_tracks:
        return output

    from ..recommend.store_runtime import open_recommendation_store_connection

    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return output
    try:
        entity_keys = list(catalog_key_to_tracks)
        placeholders = ",".join("?" for _ in entity_keys)
        rows = connection.execute(
            f"""
            SELECT s.entity_key, s.source_provider, s.source_key, s.source_authority,
                   s.confidence, s.payload_json, s.updated_at,
                   e.payload_json AS canonical_payload, e.display_title, e.display_artist
            FROM catalog_entity_sources s
            LEFT JOIN catalog_entities e ON e.entity_type = s.entity_type AND e.entity_key = s.entity_key
            WHERE s.entity_type = 'track' AND s.entity_key IN ({placeholders})
            ORDER BY
                CASE s.source_authority
                    WHEN 'official' THEN 7
                    WHEN 'official_artist_channel' THEN 6
                    WHEN 'topic' THEN 5
                    WHEN 'label' THEN 4
                    WHEN 'verified_catalog' THEN 3
                    WHEN 'trusted_match' THEN 2
                    ELSE 1
                END DESC,
                s.confidence DESC,
                s.updated_at DESC
            """,
            entity_keys,
        ).fetchall()
    except Exception:
        return output
    finally:
        connection.close()
    seen: dict[str, set[tuple[str, str]]] = {key: set() for key in output}
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except Exception:
            payload = {}
        state = str(payload.get("verification_state") or "")
        if state != "verified":
            continue
        from ..discovery.source_registry import playback_source_semantically_compatible

        try:
            canonical = json.loads(str(row['canonical_payload'] or '{}'))
            canonical['title'] = canonical.get('title') or row['display_title'] or ''
            canonical['artist'] = canonical.get('artist') or row['display_artist'] or ''
        except (ValueError, TypeError, AttributeError, KeyError, IndexError):
            canonical = {}
        if not playback_source_semantically_compatible(payload, canonical):
            continue
        provider = str(row["source_provider"] or "").strip().lower()
        source_id = str(row["source_key"] or "").strip()
        if provider == "youtube" and not re.fullmatch(r"[A-Za-z0-9_-]{11}", source_id):
            continue
        if provider == "audius" and not source_id:
            continue
        if provider not in {"youtube", "audius"}:
            continue
        identity = (provider, source_id)
        entity_key = str(row["entity_key"] or "")
        for track_key in catalog_key_to_tracks.get(entity_key, []):
            if identity in seen[track_key]:
                continue
            seen[track_key].add(identity)
            output[track_key].append({
                "provider": provider,
                "source_id": source_id,
                "authority": str(row["source_authority"] or ""),
                "verification_state": state,
                "verified_at": str(payload.get("verified_at") or ""),
            })
    for sources in output.values():
        sources.sort(
            key=lambda item: (
                item.get("verification_state") == "verified",
                item.get("authority")
                in {"official", "official_artist_channel", "topic", "label"},
            ),
            reverse=True,
        )
    return output


def playback_source_candidates(server: Any, track_key: str) -> list[dict[str, str]]:
    value = str(track_key or "").strip()
    return playback_source_candidates_batch(server, [value]).get(value, [])


def _playback_path(track_key: str) -> str:
    return f"/playback/stream/{urllib.parse.quote(str(track_key or ''), safe='')}"


def _audius_stream_url(source_id: str) -> str:
    app_name = urllib.parse.quote(os.environ.get("AUDIUS_APP_NAME", "Neatie"), safe="")
    return f"https://api.audius.co/v1/tracks/{source_id}/stream?app_name={app_name}"


def prepare_playback_tracks(
    server: Any,
    track_keys: list[str],
    *,
    limit: int,
    current_track_key: str | None = None,
    active_queue: bool = False,
    defer_all_chunks: bool = False,
    cached_only: bool = False,
    preferred_cached_variant: str = '',
):
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in track_keys:
        key = str(raw or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
        if len(ordered) >= max(int(limit or 1), 1):
            break
    if current_track_key and current_track_key in ordered:
        ordered.remove(current_track_key)
        ordered.insert(0, current_track_key)

    selected = playback_source_candidates_batch(server, ordered)
    if cached_only:
        cache = get_stream_cache(server)
        if cache.backend_name != 'r2':
            return {}, {}

        def cached_track(sources):
            try:
                for source in sources[:2]:
                    if source['provider'] != 'youtube':
                        continue
                    source_id = source['source_id']
                    if preferred_cached_variant in {'aac-v1', 'opus-v1'}:
                        from .stream_core_runtime import preferred_cached_audio
                        metadata = preferred_cached_audio(server, source_id, preferred_cached_variant, schedule=active_queue)
                    else:
                        metadata = prepared_cached_object(server, source_id) or cache.head(source_id)
                    if not metadata or not current_audio_index(metadata) or not cached_resource_version(metadata):
                        continue
                    if active_queue:
                        schedule_cached_audio_repair(server, source_id, metadata)
                    return {**cached_stream_descriptor(cache, source_id, metadata),
                            'provider': 'youtube', 'source_id': source_id,
                            'chunk_deferred': True, 'cached_prefix_bytes': 0}
            finally:
                _cache_metadata_slots.release()

        futures = {}
        for key in ordered[:24]:
            sources = selected.get(key) or []
            if not sources or not _cache_metadata_slots.acquire(blocking=False):
                continue
            try:
                futures[_cache_metadata_workers.submit(cached_track, sources)] = key
            except Exception:
                _cache_metadata_slots.release()
        completed, _ = wait(futures, timeout=2)
        prepared = {}
        for future in completed:
            try:
                value = future.result()
                if value:
                    prepared[futures[future]] = value
            except Exception:
                pass  # Unavailable optional metadata cannot fail catalog/playback.
        return prepared, {}
    youtube_first = {
        key: sources[0]["source_id"]
        for key, sources in selected.items()
        if sources and sources[0]["provider"] == "youtube"
    }
    if preferred_cached_variant == 'aac-v1':
        from .stream_core_runtime import remember_aac_demand
        for source_id in youtube_first.values():
            remember_aac_demand(server, source_id)
    if youtube_first:
        youtube_prepared, youtube_failed = prepare_streams_with_failures(
            server,
            list(youtube_first.values()),
            limit=len(youtube_first) or 1,
            current_video_id=youtube_first.get(current_track_key or ""),
            active_queue=active_queue,
            defer_all_chunks=defer_all_chunks,
            preferred_cached_variant=preferred_cached_variant,
        )
    else:
        youtube_prepared, youtube_failed = {}, {}
    prepared: dict[str, dict[str, Any]] = {}
    failed: dict[str, dict[str, Any]] = {}
    for track_key in ordered:
        sources = selected.get(track_key) or []
        if not sources:
            failed[track_key] = {
                "code": "playback_source_missing",
                "message": "No verified playback source",
            }
            continue
        resolved = None
        last_failure: dict[str, Any] | None = None
        for index, source in enumerate(sources):
            provider = source["provider"]
            source_id = source["source_id"]
            if provider == "audius":
                resolved = {
                    "prepared": True,
                    "playback_path": _playback_path(track_key),
                    "playback_url": _playback_path(track_key),
                    "source_kind": "audius_proxy",
                    "provider": "audius",
                    "source_id": source_id,
                    "headers": {},
                    "resolve_ms": 0,
                    "chunk_ms": 0,
                    "cached_prefix_bytes": 0,
                    "target_chunk_bytes": 0,
                    "expires_at": time.time() + 900,
                }
                break
            metrics = youtube_prepared.get(source_id) if index == 0 else None
            failure = youtube_failed.get(source_id) if index == 0 else None
            if metrics is None and index > 0:
                retry_prepared, retry_failed = prepare_streams_with_failures(
                    server,
                    [source_id],
                    limit=1,
                    current_video_id=source_id,
                    active_queue=active_queue,
                    defer_all_chunks=defer_all_chunks,
                    preferred_cached_variant=preferred_cached_variant,
                )
                metrics = retry_prepared.get(source_id)
                failure = retry_failed.get(source_id)
            if metrics is not None:
                path = metrics.get('playback_path') or _playback_path(track_key)
                resolved = {
                    **dict(metrics),
                    "playback_path": path,
                    "playback_url": path,
                    "provider": "youtube",
                    "source_id": source_id,
                    "source_kind": "youtube_proxy",
                    "headers": {},
                }
                break
            if isinstance(failure, dict):
                last_failure = dict(failure)
        if resolved is not None:
            prepared[track_key] = resolved
        else:
            failed[track_key] = last_failure or {
                "code": "playback_source_failed",
                "message": "All verified playback sources failed",
            }
    return prepared, failed


def download_audio(server: Any, req: Any):
    out_path = os.path.join(server.DOWNLOAD_DIR, f"{req.video_id}.mp3")
    json_cache = os.path.join(server.DOWNLOAD_DIR, f"{req.video_id}.json")

    if os.path.exists(out_path) and os.path.getsize(out_path) < 100:
        os.remove(out_path)
        if os.path.exists(json_cache):
            os.remove(json_cache)

    if os.path.exists(out_path) and os.path.exists(json_cache):
        try:
            with open(json_cache, "r", encoding="utf-8") as file:
                meta = json.load(file)
                meta["message"] = "Already downloaded"
                return meta
        except Exception:
            pass

    if os.path.exists(out_path):
        ydl_opts = _ytdlp_opts(server)
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
                    "message": "Already downloaded",
                }
                with open(json_cache, "w", encoding="utf-8") as file:
                    json.dump(meta, file)
                return meta
            except Exception as exc:
                os.remove(out_path)
                raise HTTPException(status_code=500, detail=str(exc))

    ydl_opts = {
        **_ytdlp_opts(server),
        "outtmpl": os.path.join(server.DOWNLOAD_DIR, f"{req.video_id}.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    url = f"https://www.youtube.com/watch?v={req.video_id}"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
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
        except Exception as exc:
            # Downloads use the already-selected playback identity. Searching by
            # title here could silently replace it with an unrelated upload.
            raise HTTPException(status_code=500, detail=str(exc))

        try:
            with open(
                os.path.join(server.DOWNLOAD_DIR, f"{req.video_id}.json"),
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(meta, file)
        except Exception as exc:
            print(f"JSON DUMP ERROR: {exc}")
        return meta


def stream_audio(server: Any, video_id: str):
    file_path = os.path.join(server.DOWNLOAD_DIR, f"{video_id}.mp3")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found. Please download first.")
    return FileResponse(file_path, media_type="audio/mpeg", filename=f"{video_id}.mp3")


def proxy_stream(server: Any, video_id: str, request: Request):
    started = time.perf_counter()
    trace = playback_trace_context.get()
    request_id = trace['requestId'] if trace is not None else uuid.uuid4().hex[:12]
    if trace is not None:
        trace['sourceId'] = video_id[:100]
        trace['cacheVariant'] = request.query_params.get('cache_variant', '')[:20]
        trace['cacheVersion'] = request.query_params.get('cache_version', '')[:40]
    session = request.query_params.get('playback_session', '')
    session = session if re.fullmatch(r'[A-Za-z0-9_-]{1,80}', session) else 'legacy'
    try:
        response = _proxy_stream(server, video_id, request)
    except Exception as exc:
        detail = exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) else {}
        facts = detail.get('diagnostics') or {}
        if trace is not None:
            trace.update(failureCode=detail.get('code', 'stream_failed'),
                         failureStage=facts.get('stage', 'source_resolution'))
        print(f'[EBB:playback] track={video_id} session={session} request={request_id} result=failed '
              f'error={type(exc).__name__} code={detail.get("code", "stream_failed")} '
              f'stage={facts.get("stage", "source_resolution")} '
              f'httpStatus={facts.get("http_status", 0)} '
              f'ms={int((time.perf_counter()-started)*1000)}', flush=True)
        raise
    handler_ms = int((time.perf_counter()-started)*1000)
    if trace is not None:
        trace['handlerMs'] = handler_ms
    response.headers['Server-Timing'] = f'handler;dur={handler_ms}'
    response.headers['Timing-Allow-Origin'] = '*'
    response.headers['X-Neatie-Playback-Request'] = request_id
    response.headers.setdefault('Cache-Control', 'no-store')
    byte_range = request.headers.get('range', '')
    safe_range = byte_range.removeprefix('bytes=') if re.fullmatch(r'bytes=\d{0,19}-\d{0,19}', byte_range) else 'none'
    if trace is not None:
        trace['range'] = safe_range
    print(f'[EBB:playback-start] request={request_id} handlerMs={handler_ms} '
          f'status={response.status_code} source={response.headers.get("X-Auralis-Stream-Cache", "upstream")} '
          f'range={safe_range} native={int("x-playback-session-id" in request.headers)} '
          f'versioned={int(bool(request.query_params.get("cache_version")))} '
          f'media={response.headers.get("Content-Type", "unknown").split(";")[0]}', flush=True)
    if not isinstance(response, StreamingResponse):
        return response
    iterator = response.body_iterator
    async def measured():
        sent = 0
        first_ms = None
        result = 'cancelled'
        iterator_at = time.perf_counter()
        if trace is not None:
            from anyio.to_thread import current_default_thread_limiter
            stats = current_default_thread_limiter().statistics()
            trace.update(iteratorEnterMs=round((iterator_at-started)*1000, 1),
                         workersBeforeIteratorBorrowed=stats.borrowed_tokens,
                         workersBeforeIteratorTotal=stats.total_tokens,
                         workersBeforeIteratorWaiting=stats.tasks_waiting)
        try:
            while True:
                read_at = time.perf_counter()
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                if trace is not None:
                    wait_ms = round((time.perf_counter()-read_at)*1000, 1)
                    trace['iteratorReadMs'] = round(trace.get('iteratorReadMs', 0)+wait_ms, 1)
                    trace['maxIteratorReadMs'] = max(trace.get('maxIteratorReadMs', 0), wait_ms)
                if first_ms is None:
                    first_ms = int((time.perf_counter()-started)*1000)
                    if trace is not None:
                        trace['firstYieldMs'] = first_ms
                        trace['iteratorWaitMs'] = max(0, first_ms-handler_ms)
                        trace['workerReadWaitMs'] = round((time.perf_counter()-iterator_at)*1000, 1)
                    print(f'[EBB:playback-first] request={request_id} firstMs={first_ms} bytes={len(chunk)}', flush=True)
                sent += len(chunk)
                yield chunk
            result = 'complete'
        except Exception as exc:
            result = type(exc).__name__
            raise
        finally:
            # The measured wrapper owns the underlying audio iterator. Closing
            # it releases any open R2/upstream body even when the client stops
            # after a yield or the ASGI send task is cancelled.
            try:
                await _close_audio(iterator, asynchronous=True,
                                   preserve_error=sys.exc_info()[0] is not None)
            finally:
                # Suppress successful probe requests; keep failures and full transfers.
                if sent > 65536 or result not in {'complete', 'cancelled'} or os.environ.get('AURALIS_PLAYBACK_VERBOSE') == '1':
                    source = response.headers.get('X-Auralis-Stream-Cache', 'upstream')
                    print(f'[EBB:playback] track={video_id} session={session} request={request_id} source={source} '
                          f'firstMs={first_ms} ms={int((time.perf_counter()-started)*1000)} '
                          f'bytes={sent} result={result}', flush=True)
    response.body_iterator = measured()
    return response


def _proxy_stream(server: Any, video_id: str, request: Request):
    try:
        trace = playback_trace_context.get()
        if trace is not None and hasattr(server, 'stream_info_lock'):
            with server.stream_info_lock:
                existing = server.stream_info_cache.get(video_id)
                trace.update(resolvedUrlAtStart=bool(existing and existing['expires_at'] > time.time()),
                    acquisitionInFlightAtStart=video_id in server.stream_info_inflight)
            trace['prefixAtStart'] = bool(get_cached_stream_chunk(server, video_id))
        range_header = request.headers.get("range")
        session = request.query_params.get('playback_session', '')
        if session and (len(session) > 80 or not re.fullmatch(r'[A-Za-z0-9_-]+', session)):
            raise HTTPException(status_code=422, detail='Invalid playback session')
        # Only an active play session may keep us on an upstream representation.
        # Legacy resolver/preparation entries must not bypass completed audio.
        selected = selected_stream_info(server, video_id, session) if session else None
        source_version = request.query_params.get('stream_version', '')
        if source_version:
            if (not session or not re.fullmatch(r'[a-f0-9]{64}', source_version)
                    or request.query_params.get('cache_variant') or request.query_params.get('cache_version')):
                raise HTTPException(status_code=422, detail='Invalid prepared representation')
            if not selected or stream_representation_key(selected) != source_version:
                raise PlaybackSourceExpired('Prepared playback resource is unavailable')
        if selected and selected.get('compatibility'):
            raise PlaybackSourceExpired('Browser capture has been retired; start playback again')
        stream_cache = get_stream_cache(server)
        variant = request.query_params.get('cache_variant', '')
        version = request.query_params.get('cache_version', '')
        if version and not re.fullmatch(r'v1-[a-f0-9]{32}', version):
            raise HTTPException(status_code=422, detail='Invalid cached version', headers={'Cache-Control': 'no-store'})
        if version and not variant:
            raise HTTPException(status_code=422, detail='Cached versions require a representation', headers={'Cache-Control': 'no-store'})
        if variant and variant not in {'legacy', 'seek-v1', 'seek-v2', 'opus-v1', 'aac-v1'}:
            raise HTTPException(status_code=422, detail='Invalid cached representation')
        preferred = request.query_params.get('preferred_cached_variant', '')
        if preferred not in {'', 'opus-v1', 'aac-v1'}:
            raise HTTPException(status_code=422, detail='Invalid cached preference')
        from .stream_core_runtime import preferred_cached_audio
        stream_cache_meta = _delivery_stage('cacheLookupMs', lambda: (selected.get('cached_object') if selected else
            preferred_cached_audio(server, video_id, preferred, schedule=True) if preferred == 'aac-v1' and not variant else
            prepared_cached_object(server, video_id, variant)
            or (stream_cache.head_variant(video_id, variant) if variant and stream_cache.backend_name == 'r2'
                else stream_cache.head(video_id))))
        if trace is not None:
            if selected and not selected.get('cached_object'):
                trace['resolutionOrigin'] = 'pinned'
            elif stream_cache.enabled:
                trace['storedAudioAtStart'] = bool(stream_cache_meta)
        if variant and not stream_cache_meta:
            raise PlaybackSourceExpired('Cached playback resource is unavailable')
        if variant == 'opus-v1' and not current_audio_index(stream_cache_meta):
            raise PlaybackSourceExpired('Cached playback representation is unverified')
        if variant == 'aac-v1' and not current_audio_index(stream_cache_meta):
            raise PlaybackSourceExpired('Cached playback representation is unverified')
        if version and cached_resource_version(stream_cache_meta or {}) != version:
            raise PlaybackSourceExpired('Cached playback representation changed')
        if selected and variant and stream_cache_meta:
            actual_variant = cached_variant(stream_cache_meta)
            if actual_variant != variant:
                raise PlaybackSourceExpired('Cached playback representation changed')
        if stream_cache_meta:
            # Initial browser selection already reaches this owner within the
            # Play gesture. Hand off the verified immutable object here rather
            # than waiting for a separate metadata request to discover it.
            # Older clients and explicitly pinned live sources keep the relay.
            if (request.query_params.get('direct_cached') == '1'
                    and not source_version and stream_cache.backend_name == 'r2'):
                redirect_range = parse_byte_range(range_header,
                    int(stream_cache_meta.get('content_length') or 0)) if range_header else (0, None)
                # Never turn a lost unversioned session's continuation into a
                # different file. Existing expiry/version/range guards still own it.
                safe_redirect = redirect_range is not None and not (
                    session and not selected and not variant and redirect_range[0] > 0)
                retained = (selected or {}).get('cached_delivery') or {}
                delivery = ((retained if retained.get('delivery_expires_at', 0) > time.time()+60
                             else stream_cache.direct_delivery(video_id, stream_cache_meta))
                            if safe_redirect else {})
                if delivery.get('delivery_url'):
                    if session and selected is None:
                        from .stream_sources import _remember_selected_stream
                        _remember_selected_stream(server, video_id, session,
                            {'cached_object': dict(stream_cache_meta),
                             'cached_delivery': delivery, 'pinned': True},
                            time.time() + 1200)
                    if trace is not None:
                        trace.update(cacheTier='direct_redirect',
                                     cacheVariant=cached_variant(stream_cache_meta),
                                     cacheVersion=cached_resource_version(stream_cache_meta))
                    return RedirectResponse(delivery['delivery_url'], status_code=307,
                        headers={'Cache-Control': 'no-store',
                                 'X-Auralis-Stream-Cache': 'direct_redirect'})
            if stream_cache.backend_name == 'r2':
                from .stream_core_runtime import schedule_cached_audio_repair
                schedule_cached_audio_repair(server, video_id, stream_cache_meta)
            total_length = int(stream_cache_meta.get("content_length") or 0) or None
            resource_headers = {'Cache-Control': 'no-store'}
            if version:
                resource_headers = {'ETag': '"' + version + '"',
                                    'Cache-Control': 'no-store' if session else 'private, max-age=3600, immutable'}
                validators = [tag.strip().removeprefix('W/') for tag in request.headers.get('if-none-match', '').split(',')]
                if resource_headers['ETag'] in validators or '*' in validators:
                    return Response(status_code=304, headers=resource_headers)
                if request.headers.get('if-range') and request.headers['if-range'] != resource_headers['ETag']:
                    range_header = None
            parsed_range = parse_byte_range(range_header, total_length)
            if range_header and parsed_range is None:
                raise HTTPException(
                    status_code=416,
                    detail={"code": "invalid_range", "video_id": video_id},
                )
            start = parsed_range[0] if parsed_range else 0
            end = parsed_range[1] if parsed_range else None
            if (session and not selected and not variant and start > 0
                    and cached_variant(stream_cache_meta) != 'legacy'):
                # After a restart, an unversioned session may refer to the old
                # file. Ask for a new resource instead of appending new-layout
                # bytes. Prepared URLs explicitly bind their immutable variant.
                raise PlaybackSourceExpired('Playback representation must be renewed')
            headers, status_code = cached_response_headers(
                stream_cache_meta,
                start=start,
                end=end,
                ranged=bool(range_header),
            )
            headers.update(resource_headers)
            if os.environ.get('AURALIS_PLAYBACK_VERBOSE') == '1': print(
                "[EBB:stream-cache] "
                f"video={video_id} event=hit backend={stream_cache.backend_name} "
                f"bytes={total_length or 0} range={range_header or 'full'}"
            )
            prefix = get_cached_stream_chunk(server, video_id) if hasattr(server, 'stream_chunk_lock') else None
            key = cached_object_key(stream_cache_meta)
            prefix = prefix if key and prefix and prefix.get('representation_key') == key else None
            if (stream_cache.backend_name == 'r2' and not prefix and start == 0
                    and end is not None and end < 65536 and total_length
                    and hasattr(server, 'stream_chunk_inflight_lock')):
                # A small browser probe can retain useful startup bytes from
                # this same ETag-checked read for the immediately following GET.
                # Its response still contains only the exact requested range.
                from .stream_core_runtime import warm_cached_object_prefix
                prefix = _delivery_stage('prefixWarmMs', lambda: warm_cached_object_prefix(
                    server, video_id, stream_cache, stream_cache_meta))
            opened_source = None
            if stream_cache.backend_name != 'r2':
                chunks = stream_cache.iter_object(video_id, start=start, end=end)
            elif prefix and start < len(prefix['bytes']):
                last = min(end if end is not None else total_length-1, total_length-1)
                stop = min(last+1, len(prefix['bytes']))
                def cached_then_object():
                    yield prefix['bytes'][start:stop]
                    if stop <= last:
                        yield from stream_cache.iter_object(video_id, start=stop, end=last, expected=stream_cache_meta)
                chunks = cached_then_object()
                headers['X-Auralis-Stream-Cache'] = 'r2-prefix'
            else:
                try:
                    chunks = iter(stream_cache.iter_object(video_id, start=start, end=end, expected=stream_cache_meta))
                    first = _delivery_stage('objectFirstReadMs', lambda: next(chunks))
                except Exception:
                    # Choose a different transport only before this new resource
                    # has emitted any bytes. Existing object sessions stay pinned.
                    if selected is not None or version:
                        raise PlaybackSourceExpired('Cached playback resource is unavailable')
                    stream_cache.invalidate(video_id)
                    stream_cache_meta = None
                else:
                    opened_source = chunks
                    def prefixed(first, source):
                        try:
                            yield first
                            yield from source
                        finally:
                            close = getattr(source, 'close', None)
                            if close:
                                close()
                    chunks = prefixed(first, chunks)
            if stream_cache_meta:
                if session and selected is None and hasattr(server, 'stream_info_lock'):
                    with server.stream_info_lock:
                        session_key = f'selected:{session}:{video_id}'
                        existing = server.stream_info_cache.get(session_key)
                        if existing and (existing['expires_at'] <= time.time() or
                                existing['payload'].get('cached_object') != stream_cache_meta):
                            close = getattr(opened_source, 'close', None)
                            if close:
                                close()
                            raise PlaybackSourceExpired('Playback representation changed')
                        keys = [k for k in server.stream_info_cache if k.startswith('selected:')]
                        for old in keys[:-255]:
                            server.stream_info_cache.pop(old, None)
                        if existing is None:
                            server.stream_info_cache[session_key] = {
                                'expires_at': time.time()+1200,
                                'payload': {'cached_object': dict(stream_cache_meta), 'pinned': True}}
                return _audio_response(chunks, opened_source=opened_source, status_code=status_code, headers=headers,
                    media_type=playback_content_type(stream_cache_meta.get("content_type"),
                        (stream_cache_meta.get('metadata') or {}).get('audio_codec', '')))
        if stream_cache.enabled and os.environ.get('AURALIS_PLAYBACK_VERBOSE') == '1':
            print(
                "[EBB:stream-cache] "
                f"video={video_id} event=miss backend={stream_cache.backend_name} "
                f"range={range_header or 'full'}"
            )

        retry = (request.query_params.get('retry_source') == '1'
                 and request.query_params.get('background', '').lower() not in {'1', 'true', 'yes'})
        with explicit_source_retry(retry):
            stream_info = _delivery_stage('resolutionWaitMs', lambda: selected or (
                get_stream_info(server, video_id, session=session) if session else get_stream_info(server, video_id)))
        trace = playback_trace_context.get()
        if trace is not None:
            if 'resolutionId' in stream_info:
                trace['resolutionId'] = stream_info['resolutionId']
            trace['resolutionMetricsReused'] = trace.get('resolutionOrigin') in {'memory', 'pinned', 'prepared', 'shared'}
            record_resolution_metrics(trace, stream_info, timings=not trace['resolutionMetricsReused'],
                extraction=trace.get('resolutionOrigin') != 'persisted')
            if stream_info.get('expires_at'):
                trace['sourceLeaseSeconds'] = max(0, round(float(stream_info['expires_at'])-time.time(), 1))
            if stream_info.get('validated_at'):
                trace['validationAgeMs'] = max(0, round((time.time()-float(stream_info['validated_at']))*1000, 1))
        cached_chunk = get_cached_stream_chunk(server, video_id)
        if not stream_chunk_matches(cached_chunk, stream_info):
            cached_chunk = None

        if cached_chunk and int(cached_chunk.get('total_length') or 0) >= len(cached_chunk.get('bytes') or b'') > 0:
            cached_bytes = cached_chunk.get("bytes") or b""
            content_type = cached_chunk.get("content_type") or stream_info.get("mime_type") or "audio/mp4"
            total_length = cached_chunk.get("total_length")
            parsed_range = parse_byte_range(range_header, total_length)

            if not range_header or parsed_range is not None:
                start = parsed_range[0] if parsed_range else 0
                end = parsed_range[1] if parsed_range else None

                if start < len(cached_bytes):
                    cached_end = len(cached_bytes) - 1
                    requested_end = end if end is not None else cached_end
                    slice_end = min(cached_end, requested_end)
                    cached_slice = cached_bytes[start:slice_end + 1]
                    upstream_start = max(len(cached_bytes), start)
                    needs_upstream = not (
                        (end is not None and upstream_start > end)
                        or (
                            total_length is not None
                            and upstream_start >= total_length
                        )
                    )
                    def generate_cached_then_upstream():
                        if cached_slice:
                            yield cached_slice
                        if not needs_upstream:
                            return
                        # Yield the proven prefix before waiting for a network
                        # continuation. Every later range keeps this resource.
                        yield from iter_initial_read(
                            server,
                            video_id,
                            {**stream_info, 'pinned': True},
                            cached_chunk, upstream_start, end,
                        )

                    resp_headers = {"Accept-Ranges": "bytes", "X-Auralis-Stream-Cache": "prefix"}
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
                        resp_headers["Content-Length"] = str(max(end - start + 1, 0))

                    response_iter = generate_cached_then_upstream()
                    if should_cache_full_response(start, end, total_length):
                        response_iter = iter_and_store_full_stream(
                            server,
                            video_id,
                            response_iter,
                            content_type=content_type,
                            capture_duration=stream_info.get('duration') or None,
                        )

                    return _audio_response(
                        response_iter,
                        status_code=status_code,
                        headers=resp_headers,
                        media_type=playback_content_type(content_type, stream_info.get('audio_codec', '')),
                    )

        total_length = cached_chunk.get("total_length") if cached_chunk else None
        parsed_range = parse_byte_range(range_header, total_length)
        if range_header and parsed_range is None:
            raise HTTPException(
                status_code=416,
                detail={"code": "invalid_range", "video_id": video_id},
            )
        start = parsed_range[0] if parsed_range else 0
        end = parsed_range[1] if parsed_range else None
        upstream_response, stream_info = open_upstream_stream(
            server,
            video_id,
            stream_info,
            start=start,
            end=end,
        )
        total_length = extract_total_length(upstream_response.headers) or total_length

        def generate_upstream():
            chunks = iter_upstream_stream(
                server,
                video_id,
                stream_info,
                start=start,
                end=end,
                initial_response=upstream_response,
            )
            if should_cache_full_response(start, end, total_length):
                yield from iter_and_store_full_stream(
                    server,
                    video_id,
                    chunks,
                    content_type=stream_info.get("mime_type", "audio/mp4"),
                    capture_duration=stream_info.get('duration') or None,
                )
                return
            yield from chunks

        resp_headers = {"Accept-Ranges": "bytes"}
        status_code = int(upstream_response.status_code or 200)
        for header in ("content-length", "content-range"):
            value = upstream_response.headers.get(header)
            if value:
                resp_headers["-".join(part.capitalize() for part in header.split("-"))] = value
        if total_length and (stream_info.get('pinned') or stream_info.get('compatibility')):
            response_end = min(end if end is not None else total_length-1, total_length-1)
            resp_headers['Content-Length'] = str(response_end-start+1)
            status_code = 206 if range_header else 200
            resp_headers.pop('Content-Range', None)
            if range_header:
                resp_headers['Content-Range'] = f'bytes {start}-{response_end}/{total_length}'
        return _audio_response(
            generate_upstream(),
            opened_source=upstream_response,
            status_code=status_code,
            headers=resp_headers,
            media_type=playback_content_type(
                upstream_response.headers.get("content-type")
                or stream_info.get("mime_type", "audio/mp4"),
                stream_info.get('audio_codec', ''),
            ),
        )
    except PlaybackSourceExpired:
        raise HTTPException(status_code=410, detail={'code': 'playback_source_expired'}, headers={'Cache-Control': 'no-store'})
    except HTTPException:
        raise
    except Exception as exc:
        classified = _stream_failure_payload(exc)
        raise HTTPException(
            status_code=classified["status_code"],
            detail={
                "code": classified["code"],
                "message": "Audio source preparation failed",
                "video_id": video_id,
                "diagnostics": classified.get('diagnostics') or {},
            },
        )


def playback_resolve(
    server: Any,
    track_key: str,
    *,
    title: str = "",
    artist: str = "",
    album: str = "",
    duration: int = 0,
    source_id: str = "",
    source_provider: str = "youtube",
):
    sources = _delivery_stage('sourceCandidateMs', lambda: playback_source_candidates(server, track_key))
    if not sources and str(track_key or "").startswith(
        ("recording:", "musicbrainz:recording:")
    ):
        from ..discovery.source_registry import (
            resolve_user_requested_playback_source,
        )

        try:
            resolved = resolve_user_requested_playback_source(
                server,
                track_key=track_key,
                title=title,
                artist=artist,
                album=album,
                duration=duration,
                source_id=source_id,
                source_provider=source_provider,
            )
        except Exception as exc:
            print(
                "[EBB:playback-source] "
                f"track={track_key} outcome=resolver_error "
                f"error={type(exc).__name__}"
            )
            resolved = None
        if resolved is not None:
            sources = [{
                "provider": str(resolved.get("provider") or "youtube"),
                "source_id": str(resolved.get("source_id") or ""),
                "authority": str(resolved.get("authority") or "trusted_match"),
                "verification_state": "verified",
            }]
    if not sources:
        raise HTTPException(
            status_code=404,
            detail={"code": "playback_source_missing", "track_key": track_key},
        )
    failures: list[str] = []
    for source in sources:
        provider = source["provider"]
        source_id = source["source_id"]
        if provider == "audius":
            return {
                "status": "success",
                "track_key": track_key,
                "provider": provider,
                "source_id": source_id,
                "url": _playback_path(track_key),
                "headers": {},
                "source_kind": "audius_proxy",
            }
        try:
            cache = get_stream_cache(server)
            cached = cache.head(source_id)
            if cached:
                stream_info = {"mime_type": cached.get("content_type"), "duration": duration}
            else:
                stream_info = get_stream_info(server, source_id)
            return {
                "status": "success",
                "track_key": track_key,
                "provider": provider,
                "source_id": source_id,
                "url": _playback_path(track_key),
                "headers": {},
                "mime_type": stream_info.get("mime_type") or "audio/mp4",
                "duration": stream_info.get("duration") or 0,
                "expires_at": min(
                    float(stream_info.get("expires_at") or (time.time() + 1200)),
                    time.time() + 1200,
                ),
                "source_kind": f"{cache.backend_name}_proxy" if cached else "youtube_proxy",
            }
        except Exception as exc:
            failures.append(str(classify_stream_failure(exc)))
    raise HTTPException(
        status_code=502,
        detail={
            "code": "playback_sources_failed",
            "track_key": track_key,
            "failures": failures,
        },
    )


def _proxy_audius(server: Any, source_id: str, request: Request):
    headers = {}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header
    upstream = server.upstream_http.get(
        _audius_stream_url(source_id),
        headers=headers,
        stream=True,
        timeout=(5, 25),
        allow_redirects=True,
    )
    if upstream.status_code not in {200, 206}:
        upstream.close()
        raise HTTPException(
            status_code=502,
            detail={"code": "audius_stream_failed", "source_id": source_id},
        )

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    response_headers = {"Accept-Ranges": "bytes"}
    for header in ("content-length", "content-range"):
        if upstream.headers.get(header):
            response_headers["-".join(part.capitalize() for part in header.split("-"))] = upstream.headers[header]
    return _audio_response(
        generate(),
        opened_source=upstream,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type") or "audio/mpeg",
    )


def playback_stream(server: Any, track_key: str, request: Request):
    trace = playback_trace_context.get()
    if trace is not None:
        trace['trackKey'] = track_key[:160]
        trace['routeWaitMs'] = round((time.perf_counter()-request.scope['neatie_playback_started'])*1000, 1)
    sources = _delivery_stage('sourceCandidateMs', lambda: playback_source_candidates(server, track_key))
    if not sources and request.query_params.get("resolve") == "1" and not request.query_params.get("background"):
        # Direct browser playback must start within the user's tap. Reuse the
        # POST resolver here only when the canonical source is still missing.
        from ..contracts import PlaybackResolveRequest
        try:
            identity = PlaybackResolveRequest(**{**dict(request.query_params), "track_key": track_key})
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid playback identity")
        sources = [_delivery_stage('sourceResolveMs', lambda: playback_resolve(server, **identity.model_dump()))]
    if trace is not None:
        trace['sourceCandidates'] = len(sources)
    if not sources:
        raise HTTPException(
            status_code=404,
            detail={"code": "playback_source_missing", "track_key": track_key},
        )
    failures: list[str] = []
    failure_details: list[dict] = []
    background = str(request.query_params.get("background") or "").casefold() in {
        "1",
        "true",
        "yes",
    }
    youtube_blocked = False
    if background:
        from ..discovery.source_registry import youtube_background_resolution_blocked

        youtube_blocked = youtube_background_resolution_blocked(server)
    for source in sources:
        try:
            if trace is not None:
                trace['sourceAttempts'] = trace.get('sourceAttempts', 0)+1
            if source["provider"] == "audius":
                return _proxy_audius(server, source["source_id"], request)
            if background and youtube_blocked:
                failures.append("youtube_temporarily_blocked")
                continue
            return proxy_stream(server, source["source_id"], request)
        except HTTPException as exc:
            if (request.query_params.get('cache_version')
                    or request.query_params.get('stream_version')):
                # Both URLs pin one representation. Preserve 410 so the
                # player can refresh its session; another source cannot
                # satisfy this version and must not become a generic 502.
                raise
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            if background and detail.get("code") == "source_blocked":
                from ..discovery.source_registry import record_youtube_background_failures

                record_youtube_background_failures(
                    server,
                    {source["source_id"]: detail},
                )
                youtube_blocked = True
            failures.append(str(detail.get('code') or 'stream_failed'))
            failure_details.append({'source_id': source['source_id'],
                'code': detail.get('code') or 'stream_failed',
                'diagnostics': detail.get('diagnostics') or {}})
            continue
    raise HTTPException(
        status_code=502,
        detail={
            "code": "playback_sources_failed",
            "track_key": track_key,
            "failures": failures,
            "failure_details": failure_details,
        },
    )
