from __future__ import annotations

import os
import json
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Lock
from concurrent.futures import Future
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

    def _indexed_key(self, video_id: str) -> str:
        return self._key(video_id) + '.seek-v1'

    def head_variant(self, video_id: str, variant: str):
        if variant not in {'legacy', 'seek-v1'}:
            raise ValueError('Unsupported cached representation')
        key = self._key(video_id) if variant == 'legacy' else self._indexed_key(video_id)
        with self._head_cache_lock:
            cached = self._head_cache.get(_safe_video_id(video_id))
            if cached and cached[0] > time.monotonic() and cached[1] and cached[1].get('key') == key:
                return dict(cached[1])
        return self._fetch_key_head(video_id, key)

    def head(self, video_id: str) -> Optional[Dict[str, Any]]:
        safe_id = _safe_video_id(video_id)
        now = time.monotonic()
        with self._head_cache_lock:
            cached = self._head_cache.get(safe_id)
            if cached is not None and cached[0] > now:
                return dict(cached[1]) if cached[1] is not None else None
            if cached is not None:
                self._head_cache.pop(safe_id, None)
            pending = self._head_inflight.get(safe_id)
            owner = pending is None
            if owner:
                pending = Future()
                self._head_inflight[safe_id] = pending
        if not owner:
            result = pending.result(timeout=30)
            return dict(result) if result else None
        try:
            result = self._fetch_head(video_id)
            with self._head_cache_lock:
                # Upload/invalidation may retire this lookup while it is running.
                if self._head_inflight.get(safe_id) is pending:
                    ttl = self._head_hit_ttl if result is not None else self._head_miss_ttl
                    if len(self._head_cache) >= 512:
                        self._head_cache.pop(next(iter(self._head_cache)), None)
                    self._head_cache[safe_id] = (time.monotonic() + ttl, dict(result) if result else None)
            pending.set_result(result)
            return dict(result) if result else None
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        finally:
            with self._head_cache_lock:
                if self._head_inflight.get(safe_id) is pending:
                    self._head_inflight.pop(safe_id, None)

    def invalidate(self, video_id):
        with self._head_cache_lock:
            self._head_cache.pop(_safe_video_id(video_id), None)
            self._head_inflight.pop(_safe_video_id(video_id), None)

    def _fetch_head(self, video_id):
        for key in (self._indexed_key(video_id), self._key(video_id)):
            metadata = self._fetch_key_head(video_id, key)
            if metadata is not None:
                return metadata
        return None

    def _fetch_key_head(self, video_id, key):
        metadata: Optional[Dict[str, Any]] = None
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
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
            if status != 404:
                _log("head_failed", video_id, status=status, reason=type(exc).__name__)
            if status not in {403, 404}:
                raise
        except Exception as exc:
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
            if (metadata or {}).get('seek_index') == 'v1':
                # Publish an immutable derivative. Never overwrite bytes pinned
                # by an old session, and never replace an existing derivative.
                try:
                    with open(path, 'rb') as body:
                        self._client.put_object(
                            Bucket=self.bucket, Key=self._indexed_key(video_id),
                            Body=body, ContentLength=os.path.getsize(path),
                            ContentType=_content_type(content_type), Metadata=metadata,
                            IfNoneMatch='*',
                        )
                except ClientError as exc:
                    if int((exc.response or {}).get('ResponseMetadata', {}).get('HTTPStatusCode') or 0) != 412:
                        raise
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
                                   else ['-cues_to_front', '1']),
                 '-y', output], capture_output=True, timeout=45)
            if result.returncode:
                raise ValueError('capture_remux_failed')
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
    Path(temp_dir).mkdir(parents=True, exist_ok=True)
    total = 0
    temp_path = ""
    validated_path = ""
    wrote_cache_bytes = 0
    truncated = False
    stored = False
    try:
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
            if capture_duration is not None:
                try:
                    validated_path = validate_capture_file(temp_path, capture_duration, content_type)
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    _log('validation_failed', video_id, reason=reason)
                    return
            elif 'webm' in content_type or 'mp4' in content_type:
                try:
                    validated_path = normalize_source_file(temp_path, content_type)
                except Exception:
                    _log('validation_failed', video_id, reason='source_index_or_decode_invalid')
                    return
            if os.path.getsize(validated_path or temp_path) > max_bytes:
                _log('upload_skipped', video_id, reason='validated_size_exceeds_limit')
                return
            _log(
                "upload_start",
                video_id,
                backend=cache.backend_name,
                bytes=total,
            )
            stored = cache.put_file(
                video_id,
                validated_path or temp_path,
                content_type=content_type or "audio/mp4",
                metadata={"video_id": _safe_video_id(video_id),
                          "bytes": str(os.path.getsize(validated_path or temp_path)),
                          **({'seek_index': 'v1'} if validated_path else {}),
                          "validated": 'capture-v1' if validated_path else 'source'},
            )
            if stored:
                _log("upload_success", video_id, backend=cache.backend_name, bytes=total)
            else:
                _log("upload_failed", video_id, backend=cache.backend_name, bytes=total)
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
        if validated_path:
            Path(validated_path).unlink(missing_ok=True)
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
