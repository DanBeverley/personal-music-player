from __future__ import annotations

import os
import json
import hashlib
import subprocess
import tempfile
import time
import math
from urllib.parse import urlsplit
from pathlib import Path
from threading import Lock, BoundedSemaphore
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar
from typing import Any, Dict, Iterable, Optional, Tuple

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
except Exception:  # pragma: no cover - optional production dependency
    boto3 = None
    Config = None
    BotoCoreError = Exception
    ClientError = Exception


_STREAM_CACHE_CLIENTS: Dict[int, "StreamCacheBackend"] = {}
_STREAM_UPLOAD_LEASES: dict[tuple[int, str], Future] = {}
_STREAM_UPLOAD_LEASES_LOCK = Lock()
_STREAM_PUBLISH_SLOTS = BoundedSemaphore(2)
_CACHE_HEAD_READS = ThreadPoolExecutor(max_workers=4, thread_name_prefix='audio-head')
_CACHE_HEAD_SLOTS = BoundedSemaphore(8)
_HEAD_PROBE = ContextVar('audio_head_probe', default=None)


def _playback_trace():
    # Import after initialization: stream_core_runtime also imports this module.
    from .stream_core_runtime import playback_trace_context
    return playback_trace_context.get()


def opus_playback_enabled() -> bool:
    return os.environ.get('NEATIE_OPUS_PLAYBACK', '0').lower() in {'1', 'true'}


def aac_playback_enabled() -> bool:
    return os.environ.get('NEATIE_AAC_PLAYBACK', '0').lower() in {'1', 'true'}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _safe_video_id(video_id: str) -> str:
    return "".join(ch for ch in _clean(video_id) if ch.isalnum() or ch in {"_", "-"})


def _log(event: str, video_id: str, **fields: Any) -> None:
    if event in {'upload_coalesced', 'upload_buffer_start', 'upload_start'} and os.environ.get('AURALIS_PLAYBACK_VERBOSE') != '1':
        return
    safe_id = _safe_video_id(video_id)
    suffix = " ".join(
        f"{key}={value}"
        for key, value in fields.items()
        if value is not None and value != ""
    )
    if suffix:
        print(f"[EBB:stream-cache] video={safe_id} event={event} {suffix}")
    else:
        print(f"[EBB:stream-cache] video={safe_id} event={event}")


def _range_header(start: int = 0, end: Optional[int] = None) -> str:
    if end is None:
        return f"bytes={max(start, 0)}-"
    return f"bytes={max(start, 0)}-{max(end, start)}"


def _content_type(value: Any) -> str:
    cleaned = _clean(value)
    if "/" in cleaned:
        return cleaned
    if cleaned in {"m4a", "mp4", "aac"}:
        return "audio/mp4"
    if cleaned in {"mp3", "mpeg"}:
        return "audio/mpeg"
    if cleaned == "webm":
        return "audio/webm"
    return "audio/mp4"


def playback_content_type(value: Any, codec: str = '') -> str:
    """Declare a codec only when extraction or validated cache metadata proves it."""
    mime = _content_type(value)
    if mime.split(';')[0].strip().lower() == 'audio/webm' and codec == 'opus':
        return 'audio/webm; codecs="opus"'
    return mime


class StreamCacheBackend:
    enabled = False
    backend_name = "none"

    def head(self, video_id: str) -> Optional[Dict[str, Any]]:
        return None

    def iter_object(
        self,
        video_id: str,
        *,
        start: int = 0,
        end: Optional[int] = None,
    ) -> Iterable[bytes]:
        return iter(())

    def put_file(
        self,
        video_id: str,
        path: str,
        *,
        content_type: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> bool:
        return False


class NullStreamCache(StreamCacheBackend):
    pass


class R2StreamCache(StreamCacheBackend):
    enabled = True
    backend_name = "r2"

    def __init__(self, server: Any) -> None:
        if boto3 is None or Config is None:
            raise RuntimeError("boto3 dependency unavailable")
        bucket = _clean(getattr(server, "AURALIS_STREAM_CACHE_BUCKET", ""))
        account_id = _clean(getattr(server, "AURALIS_R2_ACCOUNT_ID", ""))
        access_key = _clean(getattr(server, "AURALIS_R2_ACCESS_KEY_ID", ""))
        secret_key = _clean(getattr(server, "AURALIS_R2_SECRET_ACCESS_KEY", ""))
        endpoint = _clean(getattr(server, "AURALIS_R2_ENDPOINT_URL", ""))
        if not endpoint and account_id:
            endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        if not bucket or not endpoint or not access_key or not secret_key:
            raise RuntimeError("R2 stream cache is missing bucket, endpoint/account, or credentials")

        self.bucket = bucket
        self.prefix = _clean(getattr(server, "AURALIS_STREAM_CACHE_PREFIX", "streams")) or "streams"
        self._head_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
        self._head_cache_lock = Lock()
        self._head_inflight = {}
        self._head_hit_ttl = max(
            float(getattr(server, "STREAM_CACHE_HEAD_HIT_TTL_SECONDS", 45) or 45),
            1.0,
        )
        self._head_miss_ttl = max(
            float(getattr(server, "STREAM_CACHE_HEAD_MISS_TTL_SECONDS", 8) or 8),
            1.0,
        )
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 2},
                          connect_timeout=5, read_timeout=10, max_pool_connections=12),
        )

    def _key(self, video_id: str) -> str:
        safe_id = _safe_video_id(video_id)
        return f"{self.prefix.rstrip('/')}/{safe_id}.bin"

    def _indexed_key(self, video_id: str, version: str = 'v1') -> str:
        if version not in {'v1', 'v2'}:
            raise ValueError('Unsupported audio index')
        return self._key(video_id) + '.seek-' + version

    def head_variant(self, video_id: str, variant: str):
        if variant not in {'legacy', 'seek-v1', 'seek-v2', 'opus-v1', 'aac-v1'}:
            raise ValueError('Unsupported cached representation')
        key = (self._key(video_id) if variant == 'legacy' else
               self._key(video_id) + '.' + variant if variant in {'opus-v1', 'aac-v1'} else
               self._indexed_key(video_id, variant.removeprefix('seek-')))
        with self._head_cache_lock:
            cached = self._head_cache.get(_safe_video_id(video_id))
            if cached and cached[0] > time.monotonic() and cached[1] and cached[1].get('key') == key:
                return dict(cached[1])
        return self._cached_head(
            _safe_video_id(video_id) + ':' + variant,
            lambda: self._fetch_key_head(video_id, key))

    def head(self, video_id: str) -> Optional[Dict[str, Any]]:
        return self._cached_head(_safe_video_id(video_id), lambda: self._fetch_head(video_id))

    def _cached_head(self, cache_key, fetch):
        """Coalesce both preferred-object and explicit-version metadata reads."""
        now = time.monotonic()
        with self._head_cache_lock:
            cached = self._head_cache.get(cache_key)
            if cached is not None and cached[0] > now:
                trace = _playback_trace()
                if trace is not None:
                    trace['cacheHeadMemoryHits'] = trace.get('cacheHeadMemoryHits', 0) + 1
                return dict(cached[1]) if cached[1] is not None else None
            if cached is not None:
                self._head_cache.pop(cache_key, None)
            pending = self._head_inflight.get(cache_key)
            owner = pending is None
            if owner:
                pending = Future()
                self._head_inflight[cache_key] = pending
        if not owner:
            started = time.perf_counter()
            try:
                result = pending.result(timeout=30)
                return dict(result) if result else None
            finally:
                trace = _playback_trace()
                if trace is not None:
                    trace['cacheHeadSharedWaitMs'] = trace.get('cacheHeadSharedWaitMs', 0) + round((time.perf_counter()-started)*1000, 1)
        try:
            result = fetch()
            with self._head_cache_lock:
                # Upload/invalidation may retire this lookup while it is running.
                if self._head_inflight.get(cache_key) is pending:
                    ttl = self._head_hit_ttl if result is not None else self._head_miss_ttl
                    if len(self._head_cache) >= 512:
                        self._head_cache.pop(next(iter(self._head_cache)), None)
                    self._head_cache[cache_key] = (time.monotonic() + ttl, dict(result) if result else None)
            pending.set_result(result)
            return dict(result) if result else None
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        finally:
            with self._head_cache_lock:
                if self._head_inflight.get(cache_key) is pending:
                    self._head_inflight.pop(cache_key, None)

    def invalidate(self, video_id):
        safe_id = _safe_video_id(video_id)
        with self._head_cache_lock:
            for entries in (self._head_cache, self._head_inflight):
                for key in list(entries):
                    if key == safe_id or key.startswith(safe_id + ':'):
                        entries.pop(key, None)

    def _fetch_head(self, video_id):
        trace = _playback_trace()
        head_started = time.perf_counter()
        details, detail_lock = [], Lock()
        def read(key, queued=None):
            if trace is None:
                return self._fetch_key_head(video_id, key)
            started = time.perf_counter()
            row = {'variant': ('opus-v1' if key.endswith('.opus-v1') else
                   'seek-v2' if key.endswith('.seek-v2') else
                   'seek-v1' if key.endswith('.seek-v1') else 'legacy'),
                   'startMs': round((started-head_started)*1000, 1),
                   'queueMs': round((started-queued)*1000, 1) if queued else 0}
            token = _HEAD_PROBE.set(row)
            metadata = None
            try:
                metadata = self._fetch_key_head(video_id, key)
                return metadata
            finally:
                _HEAD_PROBE.reset(token)
                row['headMs'] = round((time.perf_counter()-started)*1000, 1)
                row['result'] = ('hit' if metadata is not None else
                    'denied' if row.get('status') == 403 else
                    'error' if row.get('errorType') or row.get('status', 404) != 404 else 'missing')
                with detail_lock:
                    details.append(row)
        def publish():
            if trace is not None:
                with detail_lock:
                    completed = [dict(row) for row in details]
                rows = trace.setdefault('cacheHeads', [])
                for row in completed:
                    if len(rows) < 8:
                        rows.append(row)
                    else:
                        trace['cacheHeadDetailsDropped'] = trace.get('cacheHeadDetailsDropped', 0) + 1
        preferred = (self._key(video_id) + '.opus-v1',) if opus_playback_enabled() else ()
        keys = (*preferred, self._indexed_key(video_id, 'v2'), self._indexed_key(video_id), self._key(video_id))
        try:
            first = read(keys[0])
        except BaseException:
            publish()
            raise
        if first is not None and (not preferred or current_audio_index(first)):
            publish()
            return first
        # A cold miss used to wait for every historical key in series. Only
        # probe fallbacks concurrently after the preferred object misses, so an
        # ordinary Opus cache hit still costs exactly one HEAD. Outstanding work
        # is bounded globally; at capacity, retain the serial path.
        pending = []
        try:
            for key in keys[1:]:
                future = None
                if preferred and _CACHE_HEAD_SLOTS.acquire(blocking=False):
                    try:
                        future = _CACHE_HEAD_READS.submit(read, key, time.perf_counter())
                        future.add_done_callback(lambda _: _CACHE_HEAD_SLOTS.release())
                    except Exception:
                        _CACHE_HEAD_SLOTS.release()
                pending.append((key, future))
            for key, future in pending:
                metadata = future.result() if future is not None else read(key)
                if metadata is not None:
                    return metadata  # Preference order is independent of completion order.
        finally:
            for _, future in pending:
                if future is not None:
                    future.cancel()
            if trace is not None:
                trace['cacheHeadUnfinished'] = sum(1 for _, f in pending if f is not None and not f.done())
            publish()  # Never wait for unneeded HEADs just to complete diagnostics.
        return None

    def _fetch_key_head(self, video_id, key):
        metadata: Optional[Dict[str, Any]] = None
        observation = _HEAD_PROBE.get()
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
            if observation is not None:
                observation['status'] = int(response.get('ResponseMetadata', {}).get('HTTPStatusCode') or 200)
            metadata = {
                "backend": self.backend_name,
                "key": key,
                "content_length": int(response.get("ContentLength") or 0),
                "content_type": _content_type(response.get("ContentType")),
                "etag": str(response.get("ETag") or "").strip('"'),
                "metadata": dict(response.get("Metadata") or {}),
            }
        except ClientError as exc:
            status = int((exc.response or {}).get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)
            if observation is not None:
                observation['status'] = status
            if status != 404:
                _log("head_failed", video_id, status=status, reason=type(exc).__name__)
            if status not in {403, 404}:
                raise
        except Exception as exc:
            if observation is not None:
                observation['errorType'] = type(exc).__name__
            _log("head_failed", video_id, reason=type(exc).__name__)
            metadata = None
        return dict(metadata) if metadata is not None else None

    def iter_object(
        self,
        video_id: str,
        *,
        start: int = 0,
        end: Optional[int] = None,
        expected: Optional[Dict[str, Any]] = None,
    ) -> Iterable[bytes]:
        params = {
            "Bucket": self.bucket,
            "Key": expected.get('key', self._key(video_id)) if expected else
                   (self.head(video_id) or {}).get('key', self._key(video_id)),
            "Range": _range_header(start, end),
        }
        if expected and expected.get('etag'):
            params['IfMatch'] = '"' + expected['etag'].strip('"') + '"'
        try:
            response = self._client.get_object(**params)
        except ClientError:
            self.invalidate(video_id)
            raise
        body = response["Body"]
        try:
            if expected:
                import re
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.get('ContentRange', ''))
                total = int(expected.get('content_length') or 0)
                last = min(end if end is not None else total-1, total-1)
                if (not match or tuple(map(int, match.groups())) != (start, last, total)
                        or int(response.get('ContentLength') or 0) != last-start+1
                        or _content_type(response.get('ContentType')) != _content_type(expected.get('content_type'))
                        or expected.get('etag') and str(response.get('ETag') or '').strip('"') != expected['etag'].strip('"')):
                    self.invalidate(video_id)
                    raise ValueError('Cached audio representation changed')
            size = 32 * 1024
            received = 0
            while True:
                chunk = body.read(size)
                if not chunk:
                    break
                received += len(chunk)
                if expected and received > last-start+1:
                    raise ValueError('Cached audio exceeded range')
                yield chunk
                size = 128 * 1024
            if expected and received != last-start+1:
                raise IOError('Incomplete cached audio range')
        finally:
            body.close()

    def put_file(
        self,
        video_id: str,
        path: str,
        *,
        content_type: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> bool:
        try:
            if (metadata or {}).get('cache_variant') == 'opus-v1' and not (
                    metadata.get('validated') == 'capture-v1' and metadata.get('seek_index') == 'v2'
                    and metadata.get('audio_codec') == 'opus' and _content_type(content_type) == 'audio/webm'):
                return False
            if (metadata or {}).get('cache_variant') == 'aac-v1' and not (
                    metadata.get('validated') == 'capture-v1' and metadata.get('seek_index') == 'v2'
                    and metadata.get('audio_codec') == 'aac' and _content_type(content_type) == 'audio/mp4'
                    and 0 < cached_audio_duration({'metadata':metadata}) <= 86400):
                return False
            if (metadata or {}).get('seek_index') in {'v1', 'v2'}:
                # Publish an immutable derivative. Never overwrite bytes pinned
                # by an old session, and never replace an existing derivative.
                try:
                    with open(path, 'rb') as body:
                        self._client.put_object(
                            Bucket=self.bucket, Key=(self._key(video_id) + '.' + metadata['cache_variant']
                                if (metadata.get('cache_variant'), metadata.get('audio_codec'), _content_type(content_type))
                                in {('opus-v1', 'opus', 'audio/webm'), ('aac-v1', 'aac', 'audio/mp4')}
                                else self._indexed_key(video_id, metadata['seek_index'])),
                            Body=body, ContentLength=os.path.getsize(path),
                            ContentType=_content_type(content_type), Metadata=metadata,
                            IfNoneMatch='*',
                        )
                except ClientError as exc:
                    if int((exc.response or {}).get('ResponseMetadata', {}).get('HTTPStatusCode') or 0) != 412:
                        raise
                    if metadata.get('cache_variant') == 'aac-v1':
                        self.invalidate(video_id)
                        existing = self.head_variant(video_id, 'aac-v1')
                        return bool(existing and current_audio_index(existing) and cached_resource_version(existing))
                self.invalidate(video_id)
                return True
            self._client.upload_file(
                path,
                self.bucket,
                self._key(video_id),
                ExtraArgs={
                    "ContentType": _content_type(content_type),
                    "Metadata": metadata or {},
                },
            )
            # A fresh HEAD provides the real object validator. Never warm/join
            # prefixes against guessed upload metadata with an empty ETag.
            self.invalidate(video_id)
            return True
        except (BotoCoreError, ClientError, OSError):
            return False

    def direct_delivery(self, video_id: str, metadata: Dict[str, Any]) -> dict:
        """Sign only validated derivatives whose sole publisher is create-only.

        Legacy uploads are mutable and must retain the ETag-checked relay.
        Never log the URL or persist this short-lived browser capability.
        """
        if os.environ.get('NEATIE_DIRECT_CACHED_AUDIO', '0').lower() not in {'1', 'true'}:
            return {}
        variant = cached_variant(metadata)
        version = cached_resource_version(metadata)
        if (variant not in {'aac-v1', 'opus-v1'} or not version
                or metadata.get('key') != self._key(video_id) + '.' + variant
                or not current_audio_index(metadata)):
            return {}
        # At least the complete recording plus a resume/seek margin. A new
        # selection renews near-expiry URLs; the canonical relay remains valid.
        ttl = min(86400, max(3600, math.ceil(cached_audio_duration(metadata)) + 600))
        try:
            url = self._client.generate_presigned_url('get_object', Params={
                'Bucket': self.bucket, 'Key': metadata['key'],
                'ResponseContentType': _content_type(metadata['content_type']),
                'ResponseContentDisposition': 'inline; filename="' + version + '.' + variant + '"',
                'ResponseCacheControl': 'private, max-age=' + str(ttl),
            }, ExpiresIn=ttl)
            origin = urlsplit(url)
            if (origin.scheme != 'https' or origin.username or origin.password
                    or not origin.hostname or not origin.hostname.endswith('.r2.cloudflarestorage.com')):
                return {}
            return {'delivery_url': url, 'delivery_expires_at': time.time() + ttl,
                    'delivery_version': version}
        except Exception as exc:
            _log('delivery_sign_failed', video_id, reason=type(exc).__name__)
            return {}


def get_stream_cache(server: Any) -> StreamCacheBackend:
    server = getattr(server, "raw", server)
    key = id(server)
    cached = _STREAM_CACHE_CLIENTS.get(key)
    if cached is not None:
        return cached

    backend = _clean(getattr(server, "AURALIS_STREAM_CACHE_BACKEND", "none")).lower()
    if backend != "r2":
        cache: StreamCacheBackend = NullStreamCache()
    else:
        try:
            cache = R2StreamCache(server)
            print(
                "[EBB:stream-cache] backend=r2 status=enabled "
                f"bucket={cache.bucket} prefix={cache.prefix}"
            )
        except Exception as exc:
            print(f"[EBB:stream-cache] backend=r2 status=disabled reason={exc}")
            cache = NullStreamCache()

    _STREAM_CACHE_CLIENTS[key] = cache
    return cache


def cached_resource_version(metadata: Dict[str, Any]) -> str:
    """Bind a reusable browser URL to one exact object representation."""
    if not metadata.get('key') or not metadata.get('etag') or int(metadata.get('content_length') or 0) <= 0:
        return ''
    identity = [metadata['key'], metadata['etag'], int(metadata['content_length']),
                _content_type(metadata.get('content_type'))]
    return 'v1-' + hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()[:32]


def cached_variant(metadata: Dict[str, Any]) -> str:
    key = str(metadata.get('key') or '')
    return next((variant for variant in ('opus-v1', 'aac-v1', 'seek-v2', 'seek-v1') if key.endswith('.' + variant)), 'legacy')


def cached_audio_duration(metadata: Dict[str, Any]) -> float:
    try:
        value = float((metadata.get('metadata') or {}).get('duration') or 0)
        return value if math.isfinite(value) and value > 0 else 0
    except (TypeError, ValueError):
        return 0


def current_audio_index(metadata: Dict[str, Any]) -> bool:
    tags = metadata.get('metadata') or {}
    mime = _content_type(metadata.get('content_type')).split(';')[0].lower()
    if cached_variant(metadata) == 'opus-v1':
        return (tags.get('validated') == 'capture-v1' and tags.get('seek_index') == 'v2'
                and tags.get('audio_codec') == 'opus' and mime == 'audio/webm')
    if cached_variant(metadata) == 'aac-v1':
        return (tags.get('cache_variant') == 'aac-v1' and tags.get('validated') == 'capture-v1' and tags.get('seek_index') == 'v2'
                and tags.get('audio_codec') == 'aac' and mime == 'audio/mp4'
                and 0 < cached_audio_duration(metadata) <= 86400)
    return tags.get('validated') == 'capture-v1' and (
        tags.get('seek_index') == 'v2' and mime.startswith('audio/') or
        tags.get('seek_index') == 'v1' and mime.startswith('audio/') and mime != 'audio/mp4')


def indexed_content_type(path: str, original: str) -> str:
    # The remux selects one audio stream, even if its original was video/mp4.
    return 'audio/mp4' if path.endswith('.m4a') else 'audio/webm' if path.endswith('.webm') else _content_type(original)


def cached_response_headers(
    metadata: Dict[str, Any],
    *,
    start: int = 0,
    end: Optional[int] = None,
    ranged: bool = False,
) -> Tuple[Dict[str, str], int]:
    total_length = int(metadata.get("content_length") or 0)
    content_type = _content_type(metadata.get("content_type"))
    headers = {
        "Accept-Ranges": "bytes",
        "X-Auralis-Stream-Cache": str(metadata.get("backend") or "hit"),
        "Content-Type": content_type,
    }
    if total_length <= 0:
        return headers, 206 if ranged else 200

    response_end = end if end is not None else total_length - 1
    response_end = min(max(response_end, start), total_length - 1)
    if ranged:
        headers["Content-Range"] = f"bytes {start}-{response_end}/{total_length}"
        headers["Content-Length"] = str(max(response_end - start + 1, 0))
        return headers, 206
    headers["Content-Length"] = str(total_length)
    return headers, 200


def should_cache_full_response(
    start: int,
    end: Optional[int],
    total_length: Optional[int] = None,
) -> bool:
    if start > 0:
        return False
    if end is None:
        return True
    if total_length is not None and total_length > 0:
        return end >= total_length - 1
    return False


def validate_capture_file(path: str, duration: float, content_type: str, *, remux: bool = True) -> str:
    """Validate decoded coverage and rebuild MSE containers without changing live bytes."""
    if not duration or duration <= 0:
        raise ValueError('capture_duration_missing')
    output = path + ('.webm' if 'webm' in content_type else '.m4a') if remux else path
    try:
        if remux:
            result = subprocess.run(
                ['ffmpeg', '-nostdin', '-v', 'error', '-i', path, '-map', '0:a:0',
                 '-c:a', 'copy', *(['-movflags', '+faststart'] if output.endswith('.m4a')
                                   else ['-cluster_time_limit', '1000', '-cues_to_front', '1']),
                 '-y', output], capture_output=True, timeout=45)
            if result.returncode:
                raise ValueError('capture_remux_failed')
            if output.endswith('.m4a'):
                from .mp4_audio import rechunk_aac
                rechunk_aac(output)
        checked = subprocess.run(
            ['ffmpeg', '-nostdin', '-v', 'error', '-i', output, '-map', '0:a:0',
             '-progress', 'pipe:1', '-f', 'null', '-'], capture_output=True, timeout=45)
        times = [int(line.split('=')[1]) / 1_000_000
                 for line in checked.stdout.decode().splitlines()
                 if line.startswith('out_time_us=') and line.split('=')[1].isdigit()]
        if checked.returncode or checked.stderr.strip() or not times or abs(max(times) - duration) > 2:
            raise ValueError('capture_decode_or_duration_invalid')
        return output
    except BaseException:
        if remux:
            Path(output).unlink(missing_ok=True)
        raise


def normalize_source_file(path: str, content_type: str) -> str:
    """Index complete direct media too; a successful transfer is not a seek index."""
    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', path],
        capture_output=True, timeout=30,
    )
    if probe.returncode:
        raise ValueError('source_duration_probe_failed')
    duration = float(json.loads(probe.stdout)['format']['duration'])
    return validate_capture_file(path, duration, content_type)


def prepare_opus_file(path: str) -> tuple[str, dict]:
    """Background-only immutable derivative; never part of a cold first read."""
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
        'format=duration:stream=codec_name,codec_type', '-of', 'json', path],
        capture_output=True, timeout=30)
    if probe.returncode:
        raise ValueError('opus_source_probe_failed')
    facts = json.loads(probe.stdout)
    audio = [s for s in facts.get('streams', []) if s.get('codec_type') == 'audio']
    duration = float(facts['format']['duration'])
    if len(audio) != 1 or not 0 < duration <= 7200:
        raise ValueError('opus_source_invalid')
    output = path + '.opus.webm'
    try:
        codec = ['-c:a', 'copy'] if audio[0].get('codec_name') == 'opus' else [
            '-c:a', 'libopus', '-b:a', '128k', '-vbr', 'off', '-application', 'audio',
            '-compression_level', '5', '-frame_duration', '20']
        converted = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1',
            '-i', path, '-map', '0:a:0', '-vn', '-sn', '-dn', '-map_metadata', '-1',
            *codec, '-threads', '1', '-cluster_time_limit', '1000', '-cues_to_front', '1',
            '-y', output], capture_output=True, timeout=90)
        if converted.returncode:
            raise ValueError('opus_conversion_failed')
        selected = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
            'format=duration:stream=codec_name,codec_type', '-of', 'json', output],
            capture_output=True, timeout=30)
        if selected.returncode:
            raise ValueError('opus_output_probe_failed')
        selected_facts = json.loads(selected.stdout)
        selected_audio = [s for s in selected_facts.get('streams', []) if s.get('codec_type') == 'audio']
        selected_duration = float(selected_facts['format']['duration'])
        if (len(selected_audio) != 1 or selected_audio[0].get('codec_name') != 'opus'
                or abs(selected_duration-duration) > .25):
            raise ValueError('opus_output_invalid')
        validate_capture_file(output, duration, 'audio/webm', remux=False)
        with open(path, 'rb') as parent:
            parent_hash = hashlib.file_digest(parent, 'sha256').hexdigest()
        return output, {'validated': 'capture-v1', 'seek_index': 'v2',
            'cache_variant': 'opus-v1', 'audio_codec': 'opus',
            'duration': str(selected_duration), 'parent_sha256': parent_hash}
    except BaseException:
        Path(output).unlink(missing_ok=True)
        raise


def _schedule_demanded_aac(server, cache, video_id):
    if not aac_playback_enabled() or not hasattr(server, 'stream_info_lock'):
        return
    with server.stream_info_lock:
        demand = server.stream_info_cache.get('aac-demand:' + video_id)
        requested = demand and demand['expires_at'] > time.time()
    if requested:
        from .stream_core_runtime import schedule_cached_audio_repair
        try:
            head = cache.head(video_id)
            if head:
                schedule_cached_audio_repair(server, video_id, head, target_variant='aac-v1')
        except Exception:
            pass  # The normal completed resource remains valid.


def _publish_stream_file(server, cache, video_id, temp_path, content_type, capture_duration, max_bytes):
    validated_path = ''
    try:
        with open(temp_path, 'rb') as source:
            source_hash = hashlib.file_digest(source, 'sha256').hexdigest()
        existing = cache.head(video_id)
        if (existing and current_audio_index(existing)
                and (not opus_playback_enabled() or cached_variant(existing) == 'opus-v1')
                and (existing.get('metadata') or {}).get('source_sha256') == source_hash):
            _log('upload_reused', video_id, backend=cache.backend_name)
            _schedule_demanded_aac(server, cache, video_id)
            return True
        if capture_duration is not None:
            validated_path = validate_capture_file(temp_path, capture_duration, content_type)
        elif 'webm' in content_type or 'mp4' in content_type:
            validated_path = normalize_source_file(temp_path, content_type)
        path = validated_path or temp_path
        if os.path.getsize(path) > max_bytes:
            return False
        duration = capture_duration
        native_opus = False
        if validated_path:
            probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                'format=duration:stream=codec_name,codec_type', '-of', 'json', path],
                capture_output=True, timeout=30)
            if probe.returncode:
                return False
            facts = json.loads(probe.stdout)
            duration = float(facts['format']['duration'])
            audio = [s for s in facts.get('streams', []) if s.get('codec_type') == 'audio']
            native_opus = (opus_playback_enabled() and path.endswith('.webm')
                and len(audio) == 1 and audio[0].get('codec_name') == 'opus')
        stored = cache.put_file(video_id, path,
            content_type=indexed_content_type(validated_path, content_type) if validated_path else content_type,
            metadata={'video_id': _safe_video_id(video_id), 'bytes': str(os.path.getsize(path)),
                'source_sha256': source_hash,
                **({'cache_variant': 'opus-v1', 'audio_codec': 'opus'} if native_opus else {}),
                **({'seek_index': 'v2'} if validated_path else {}),
                **({'duration': str(duration)} if duration else {}),
                'validated': 'capture-v1' if validated_path else 'source'})
        _log('upload_success' if stored else 'upload_failed', video_id,
             backend=cache.backend_name, bytes=os.path.getsize(path))
        if stored and opus_playback_enabled() and not native_opus:
            from .stream_core_runtime import schedule_cached_audio_repair
            try:
                head = cache.head(video_id)
                if head:
                    schedule_cached_audio_repair(server, video_id, head)
            except Exception:
                pass  # Optional upgrade cannot invalidate a completed upload.
        if stored:
            _schedule_demanded_aac(server, cache, video_id)
        return stored
    except Exception as exc:
        _log('validation_failed', video_id, reason=type(exc).__name__)
        return False
    finally:
        if validated_path:
            Path(validated_path).unlink(missing_ok=True)


def iter_and_store_full_stream(
    server: Any,
    video_id: str,
    chunks: Iterable[bytes],
    *,
    content_type: str,
    capture_duration: Optional[float] = None,
    max_bytes_override: Optional[int] = None,
    independent: bool = False,
) -> Iterable[bytes]:
    cache = get_stream_cache(server)
    if not cache.enabled:
        yield from chunks
        return

    raw_server = getattr(server, "raw", server)
    upload_key = (id(raw_server), _safe_video_id(video_id))
    with _STREAM_UPLOAD_LEASES_LOCK:
        predecessor = _STREAM_UPLOAD_LEASES.get(upload_key)
        owns_upload = predecessor is None
        if owns_upload:
            lease = Future()
            _STREAM_UPLOAD_LEASES[upload_key] = lease
    if not owns_upload and not independent:
        # Multiple ExoPlayer probes/range retries may overlap before the first
        # full response reaches R2. They still receive their upstream bytes,
        # but only one response may spool and upload the object.
        _log("upload_coalesced", video_id, backend=cache.backend_name)
        yield from chunks
        return

    min_bytes = int(getattr(server, "AURALIS_STREAM_CACHE_MIN_BYTES", 65536) or 65536)
    max_bytes = int(getattr(server, "AURALIS_STREAM_CACHE_MAX_BYTES", 80 * 1024 * 1024) or 80 * 1024 * 1024)
    if max_bytes_override is not None:
        max_bytes = min(max_bytes, max(0, int(max_bytes_override)))
    temp_dir = _clean(getattr(server, "AURALIS_STREAM_CACHE_TMP_DIR", "")) or tempfile.gettempdir()
    total = 0
    temp_path = ""
    wrote_cache_bytes = 0
    truncated = False
    stored = False
    try:
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=f"auralis_{_safe_video_id(video_id)}_", suffix=".bin", dir=temp_dir, delete=False) as file:
            temp_path = file.name
            _log("upload_buffer_start", video_id, backend=cache.backend_name)
            for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total <= max_bytes:
                    file.write(chunk)
                    wrote_cache_bytes += len(chunk)
                else:
                    truncated = True
                yield chunk
        if min_bytes <= total <= max_bytes:
            if not owns_upload:
                # A queue download must survive the foreground reader closing.
                # Keep its bounded spool, but avoid duplicate validation/upload
                # if the earlier writer succeeds. Never wait on playback.
                deadline = time.monotonic() + 90
                while not owns_upload:
                    if predecessor.result(timeout=max(.01, deadline-time.monotonic())):
                        return
                    with _STREAM_UPLOAD_LEASES_LOCK:
                        predecessor = _STREAM_UPLOAD_LEASES.get(upload_key)
                        if predecessor is None:
                            lease = Future()
                            _STREAM_UPLOAD_LEASES[upload_key] = lease
                            owns_upload = True
                    if not owns_upload and time.monotonic() >= deadline:
                        return
            executor = getattr(raw_server, 'stream_warm_executor', None)
            if not independent and executor is not None:
                # Playback completes before decode/upload. Transfer the bounded
                # spool and its existing lease to the warm owner exactly once.
                if not _STREAM_PUBLISH_SLOTS.acquire(blocking=False):
                    return
                path, publishing_lease = temp_path, lease
                cleaned = False
                cleanup_lock = Lock()
                def cleanup(result):
                    nonlocal cleaned
                    with cleanup_lock:
                        if cleaned:
                            return
                        cleaned = True
                    try:
                        Path(path).unlink(missing_ok=True)
                    finally:
                        with _STREAM_UPLOAD_LEASES_LOCK:
                            if _STREAM_UPLOAD_LEASES.get(upload_key) is publishing_lease:
                                _STREAM_UPLOAD_LEASES.pop(upload_key, None)
                        publishing_lease.set_result(result)
                        _STREAM_PUBLISH_SLOTS.release()
                def publish(path=path, publishing_lease=publishing_lease):
                    result = False
                    try:
                        result = _publish_stream_file(raw_server, cache, video_id, path,
                                                     content_type, capture_duration, max_bytes)
                    finally:
                        cleanup(result)
                owns_upload, temp_path = False, ''
                try:
                    future = executor.submit(publish)
                    future.add_done_callback(lambda f: cleanup(False) if f.cancelled() else None)
                except Exception:
                    cleanup(False)
                return
            stored = _publish_stream_file(raw_server, cache, video_id, temp_path,
                                          content_type, capture_duration, max_bytes)
        elif total < min_bytes:
            _log(
                "upload_skipped",
                video_id,
                backend=cache.backend_name,
                reason="below_min_bytes",
                bytes=total,
                min_bytes=min_bytes,
            )
        else:
            _log(
                "upload_skipped",
                video_id,
                backend=cache.backend_name,
                reason="above_max_bytes",
                bytes=total,
                max_bytes=max_bytes,
                wrote_bytes=wrote_cache_bytes,
                truncated=truncated,
            )
    except Exception as exc:
        _log(
            "upload_exception",
            video_id,
            backend=cache.backend_name,
            reason=exc.__class__.__name__,
        )
        raise
    finally:
        if owns_upload:
            with _STREAM_UPLOAD_LEASES_LOCK:
                if _STREAM_UPLOAD_LEASES.get(upload_key) is lease:
                    _STREAM_UPLOAD_LEASES.pop(upload_key, None)
            lease.set_result(stored)
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
