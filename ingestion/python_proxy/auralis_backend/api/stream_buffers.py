"""Version-bound startup bytes and retained initial upstream reads."""
from __future__ import annotations

import json
import hashlib
import time
import urllib.parse
import re
from threading import (Lock, Timer)
from concurrent.futures import (Future)
from typing import (Any, Optional)


from .stream_cache import (cached_variant, current_audio_index)


_initial_reads = {}
_initial_reads_lock = Lock()

def _close_initial_read(entry):
    timer = entry.get('timer')
    if timer is not None:
        timer.cancel()
    entry['response'].close()


def _expire_initial_read(key, entry):
    with _initial_reads_lock:
        owned = _initial_reads.get(key) is entry
        if owned:
            _initial_reads.pop(key)
    if owned:
        _close_initial_read(entry)


def _keep_initial_read(server, video_id, info, response, chunks, offset, last, total, mime, etag):
    key = (id(server.stream_chunk_cache), video_id, stream_representation_key(info))
    entry = dict(response=response, chunks=chunks, offset=offset, last=last,
                 total=total, mime=mime, etag=etag, until=time.monotonic()+5)
    timer = Timer(5, _expire_initial_read, args=(key, entry))
    timer.daemon = True
    entry['timer'] = timer
    retired = []
    with _initial_reads_lock:
        previous = _initial_reads.pop(key, None)
        if previous:
            retired.append(previous)
        while len(_initial_reads) >= 2:
            retired.append(_initial_reads.pop(next(iter(_initial_reads))))
        _initial_reads[key] = entry
    for old in retired:
        _close_initial_read(old)
    timer.start()


def _take_initial_read(server, video_id, info, chunk, start):
    key = (id(server.stream_chunk_cache), video_id, stream_representation_key(info))
    with _initial_reads_lock:
        entry = _initial_reads.pop(key, None)
    if entry is None:
        return None
    if (time.monotonic() >= entry['until'] or entry['offset'] > start
            or start > entry['last'] or entry['total'] != chunk.get('total_length')
            or entry['mime'] != chunk.get('content_type') or entry['etag'] != chunk.get('etag')):
        _close_initial_read(entry)
        return None
    entry['timer'].cancel()
    return entry


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
        server.stream_chunk_cache.pop(video_id, None)
        while server.stream_chunk_cache and (len(server.stream_chunk_cache) >= 128 or
                sum(len(entry['payload'].get('bytes') or b'') for entry in server.stream_chunk_cache.values())
                + len(payload.get('bytes') or b'') > 32 * 1024 * 1024):
            server.stream_chunk_cache.pop(next(iter(server.stream_chunk_cache)))
        server.stream_chunk_cache[video_id] = {
            "payload": payload,
            "expires_at": time.time() + server.STREAM_CHUNK_TTL_SECONDS,
        }


def cached_object_key(metadata):
    if not metadata.get('etag'):
        return ''
    value = ['r2', metadata.get('key'), metadata['etag'],
             metadata.get('content_length'), metadata.get('content_type')]
    return hashlib.sha256(json.dumps(value, separators=(',', ':')).encode()).hexdigest()


def warm_cached_object_prefix(server, video_id, cache, metadata, target_bytes=65536):
    key = cached_object_key(metadata)
    if not key:
        return None
    total = int(metadata['content_length'])
    aac = cached_variant(metadata) == 'aac-v1'
    # One 128-KiB AAC GET normally holds both the index and initial chunks.
    # Larger indexes extend adaptively below, with a strict 256-KiB ceiling.
    target = min(max(target_bytes, 131072) if aac else min(65536, target_bytes),
                 262144 if aac else 65536, total)
    while True:
        existing = get_cached_stream_chunk(server, video_id)
        if existing and float(existing.get('object_proven_until') or 0) <= time.time():
            existing = None
        if (existing and existing.get('representation_key') == key and len(existing['bytes']) >= target
                and (not aac or existing.get('startup_ready'))):
            return existing
        with server.stream_chunk_inflight_lock:
            pending = server.stream_chunk_inflight.get(video_id)
            owner = pending is None
            if owner:
                pending = Future()
                server.stream_chunk_inflight[video_id] = pending
        if not owner:
            pending.result(timeout=30)
            continue
        try:
            data = (existing['bytes'] if existing and existing.get('representation_key') == key else b'')
            if len(data) < target:
                extra = b''.join(cache.iter_object(video_id, start=len(data), end=target-1, expected=metadata))
                if len(extra) != target-len(data):
                    raise ValueError('Invalid cached startup prefix')
                data += extra
            ready = not aac
            if aac:
                from .mp4_audio import aac_startup_extent
                # At most the initial prefix, the remaining index, and audio.
                # Read extensions only; retain the same ETag/range proof.
                for _ in range(3):
                    required, complete = aac_startup_extent(data, total)
                    if required <= len(data):
                        ready = complete
                        break
                    extra = b''.join(cache.iter_object(video_id, start=len(data), end=required-1, expected=metadata))
                    if len(extra) != required-len(data):
                        raise ValueError('Invalid cached startup extension')
                    data += extra
                else:
                    required, complete = aac_startup_extent(data, total)
                    ready = complete and required <= len(data)
            result = {'bytes': data, 'representation_key': key, 'total_length': metadata['content_length'],
                      'content_type': metadata['content_type'], 'etag': metadata['etag'],
                      'cached_object': dict(metadata), 'object_proven_until': time.time()+1200,
                      'startup_ready': ready}
            store_stream_chunk(server, video_id, result)
            pending.set_result(result)
            return result
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        finally:
            with server.stream_chunk_inflight_lock:
                if server.stream_chunk_inflight.get(video_id) is pending:
                    server.stream_chunk_inflight.pop(video_id, None)


def prepared_cached_object(server, video_id, variant=''):
    """Reuse a GET/ETag-proven prefix; continuation still uses If-Match.

    Unversioned starts only reuse indexed objects. Active sessions keep their
    selected representation, including when a newer object is published.
    """
    if not hasattr(server, 'stream_chunk_lock'):
        return None
    prefix = get_cached_stream_chunk(server, video_id)
    if not prefix or float(prefix.get('object_proven_until') or 0) <= time.time():
        return None
    metadata = prefix.get('cached_object') or {}
    key = cached_object_key(metadata)
    if (not key or key != prefix.get('representation_key')
            or metadata.get('backend') != 'r2' or not prefix.get('bytes')):
        return None
    actual = cached_variant(metadata)
    if ((variant and variant != actual)
            or (not variant and actual == 'aac-v1')
            or (not variant and not current_audio_index(metadata))):
        return None
    return dict(metadata)


def stream_representation_key(stream_info: dict) -> str:
    """Bind a prefix to one resource without retaining credentials in its key."""
    url = str(stream_info.get("url") or "")
    if not url:
        return ""
    if stream_info.get("compatibility"):
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not any(key == "capture" and value for key, value in query):
            return ""  # Legacy captures lack a stable byte-representation identity.
        url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(
            [(key, value) for key, value in query if key not in {"background", "priority"}]
        )))
    headers = sorted((str(key).lower(), str(value))
                     for key, value in (stream_info.get("headers") or {}).items()
                     if str(key).lower() not in {"range", "if-range"})
    value = [url, headers, str(stream_info.get("mime_type") or "").split(";")[0],
             bool(stream_info.get("compatibility"))]
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def stream_chunk_matches(chunk: dict | None, stream_info: dict) -> bool:
    key = stream_representation_key(stream_info)
    return bool(chunk and key and chunk.get("representation_key") == key)


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


def warm_initial_stream_chunk(server: Any, video_id: str, stream_info: dict, target_bytes: int):
    # The caller already assigns an intent-weighted prefix size. Clamping every
    # request back to the old global 768 KiB target erased that optimization and
    # made a one-track tap download far more data than playback needs to start.
    target_bytes = max(int(target_bytes or 0), 64 * 1024)
    deadline = time.monotonic() + 25
    while True:
        cached = get_cached_stream_chunk(server, video_id, min_bytes=target_bytes)
        if stream_chunk_matches(cached, stream_info):
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Audio startup preparation timed out')
            pending.result(timeout=remaining)
            continue

        try:
            existing = get_cached_stream_chunk(server, video_id)
            if not stream_chunk_matches(existing, stream_info):
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
                content_range = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",
                                             req.headers.get("content-range", ""))
                if content_range:
                    if (req.status_code != 206 or int(content_range[1]) != start_offset
                            or int(content_range[2]) < start_offset
                            or int(content_range[2]) >= int(content_range[3])):
                        raise ValueError("Invalid preparation byte range")
                    if total_length is not None and int(content_range[3]) != total_length:
                        raise ValueError("Audio representation length changed")
                elif start_offset or stream_info.get("compatibility") or req.status_code != 200:
                    raise ValueError("Preparation did not honor the byte range")
                response_type = str(req.headers.get("content-type") or "").split(";")[0]
                expected_type = str(stream_info.get("mime_type") or "").split(";")[0]
                if existing and existing.get('etag') and req.headers.get('etag') != existing['etag']:
                    raise ValueError("Audio representation validator changed")
                if (response_type.startswith(("text/", "application/json"))
                        or response_type and expected_type and response_type != expected_type):
                    raise ValueError("Audio representation content type changed")
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

                declared_total = extract_total_length(req.headers) or total_length
                if declared_total is not None and total_bytes > declared_total:
                    raise ValueError("Preparation exceeded audio length")
                payload = {
                    "bytes": b"".join(chunks),
                    "content_type": req.headers.get(
                        "content-type",
                        stream_info.get("mime_type") or "audio/mp4",
                    ),
                    "total_length": declared_total,
                    "compatibility": stream_info.get("compatibility"),
                    "representation_key": stream_representation_key(stream_info),
                    "etag": req.headers.get("etag") or (existing or {}).get("etag"),
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
