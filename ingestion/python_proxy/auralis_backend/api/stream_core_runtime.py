from __future__ import annotations

import json
import hashlib
import os
import shutil
import time
import urllib.parse
import re
from threading import BoundedSemaphore, Lock, Timer, Thread, Event
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, List, Optional
from contextlib import contextmanager
from contextvars import ContextVar

import yt_dlp
import yt_dlp.plugins
from yt_dlp.cookies import YoutubeDLCookieJar

from .stream_cache import get_stream_cache, iter_and_store_full_stream

_capture_cache_slots = BoundedSemaphore(2)
_stream_store_slots = BoundedSemaphore(2)
_direct_extraction_slots = BoundedSemaphore(2)
_direct_background_slots = BoundedSemaphore(1)
_background_acquisition = ContextVar('background_acquisition', default=False)
_startup_tools_lock = Lock()
_startup_tools_thread = None
_startup_tools_stop = Event()


def start_playback_tools(server):
    """One best-effort startup task, sharing the existing bounded extractor pool."""
    global _startup_tools_thread
    if (os.environ.get('NEATIE_PLAYBACK_STARTUP', '0') != '1'
            or os.environ.get('NEATIE_DIRECT_POOL', '0') != '1'
            or not _ytdlp_opts(server).get('neatie_fast_acquisition')):
        return False
    with _startup_tools_lock:
        if _startup_tools_thread is not None:
            return False
        _startup_tools_stop.clear()
        def work():
            started = time.perf_counter()
            result = 'unavailable'
            token = _background_acquisition.set(True)
            try:
                from .youtube_acquisition import warm_generic_session
                with _ytdlp_extractor(_ytdlp_opts(server)) as ydl:
                    if not _startup_tools_stop.is_set():
                        result = 'ready' if warm_generic_session(ydl) else 'unavailable'
            except Exception as error:
                result = type(error).__name__
            finally:
                _background_acquisition.reset(token)
                if _startup_tools_stop.is_set():
                    from .youtube_acquisition import close_idle_extractors
                    close_idle_extractors()
                print(f'[EBB:playback-tools] result={result} ms={int((time.perf_counter()-started)*1000)}', flush=True)
        _startup_tools_thread = Thread(target=work, name='playback-startup', daemon=True)
        _startup_tools_thread.start()
        return True


def stop_playback_tools():
    _startup_tools_stop.set()
    from .youtube_acquisition import close_idle_extractors
    close_idle_extractors()

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


def _defer_stream_store(server, work, *args, **kwargs):
    """Resolution persistence must not delay the first playback bytes."""
    if not hasattr(server, 'stream_warm_executor') or not _stream_store_slots.acquire(blocking=False):
        return
    try:
        future = server.stream_warm_executor.submit(work, server, *args, **kwargs)
        future.add_done_callback(lambda _: _stream_store_slots.release())
    except Exception:
        _stream_store_slots.release()


class PlaybackSourceExpired(RuntimeError):
    pass


def cache_prepared_track(server, video_id, *, max_bytes=None):
    cache = get_stream_cache(server)
    if not cache.enabled:
        return False
    head = cache.head(video_id)
    if head and int(head.get('content_length') or 0) <= 0:
        return False
    if head and head.get('metadata', {}).get('validated') == 'capture-v1' and head.get('metadata', {}).get('seek_index') == 'v1':
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


def validate_cached_track(server, video_id):
    """Check old full objects without replacing bytes used by active players."""
    import tempfile
    import subprocess
    from pathlib import Path
    from .stream_cache import validate_capture_file
    cache = get_stream_cache(server)
    head = cache.head(video_id)
    if not head:
        return False
    if head.get('metadata', {}).get('validated') == 'capture-v1' and head.get('metadata', {}).get('seek_index') == 'v1':
        return True
    proof_key = f"validated-cache:{video_id}:{head.get('etag', '')}"
    if head.get('etag'):
        with server.stream_info_lock:
            proof = server.stream_info_cache.get(proof_key)
            if proof and proof['expires_at'] > time.time():
                return proof.get('payload', {}).get('valid', True)
    length = int(head.get('content_length') or 0)
    if not 0 < length <= int(getattr(server, 'AURALIS_STREAM_CACHE_MAX_BYTES', 80*1024*1024)):
        return False
    with tempfile.TemporaryDirectory(prefix='queue-audio-validate-') as directory:
        path = str(Path(directory) / 'audio.bin')
        try:
            total = 0
            with open(path, 'wb') as file:
                for chunk in cache.iter_object(video_id, expected=head):
                    total += len(chunk)
                    if total > length:
                        return False
                    file.write(chunk)
            if total != length:
                return False
            probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                'format=duration', '-of', 'json', path], capture_output=True, timeout=30)
            duration = float(json.loads(probe.stdout)['format']['duration'])
            if cache.backend_name != 'r2':
                validate_capture_file(path, duration, head['content_type'], remux=False)
                return True
            normalized = validate_capture_file(path, duration, head['content_type'])
            # The derivative key is immutable; selected sessions retain their
            # original key/ETag/length. Preparation/new sessions pick this copy.
            if not cache.put_file(video_id, normalized, content_type=head['content_type'],
                                  metadata={'validated': 'capture-v1', 'seek_index': 'v1'}):
                return False
            if head.get('etag'):
                with server.stream_info_lock:
                    keys = [k for k in server.stream_info_cache if k.startswith('validated-cache:')]
                    for key in keys[:-127]:
                        server.stream_info_cache.pop(key, None)
                    server.stream_info_cache[proof_key] = {'expires_at': time.time()+3600, 'payload': {'valid':True}}
            return True
        except Exception:
            if head.get('etag'):
                with server.stream_info_lock:
                    keys = [k for k in server.stream_info_cache if k.startswith('validated-cache:')]
                    for key in keys[:-127]:
                        server.stream_info_cache.pop(key, None)
                    server.stream_info_cache[proof_key] = {
                        'expires_at':time.time()+30,'payload':{'valid':False}}
            return False


def schedule_cached_audio_repair(server, video_id, head):
    """Repair legacy objects off playback's response path using the warm owner."""
    if (head.get('metadata', {}).get('validated') == 'capture-v1' and head.get('metadata', {}).get('seek_index') == 'v1'
            or not hasattr(server, 'stream_warm_executor')
            or not hasattr(server, 'stream_info_inflight')):
        return
    key = f'cache-repair:{video_id}'
    with server.stream_info_lock:
        proof = server.stream_info_cache.get(f"validated-cache:{video_id}:{head.get('etag', '')}")
        if proof and proof['expires_at'] > time.time() and proof.get('payload', {}).get('valid') is False:
            return
        if key in server.stream_info_inflight or sum(
                k.startswith('cache-repair:') for k in server.stream_info_inflight) >= 2:
            return
        pending = Future()
        server.stream_info_inflight[key] = pending
    def repair():
        try:
            pending.set_result(cache_prepared_track(server, video_id))
        except Exception:
            pending.set_result(False)
        finally:
            with server.stream_info_lock:
                server.stream_info_inflight.pop(key, None)
    try:
        server.stream_warm_executor.submit(repair)
    except Exception:
        with server.stream_info_lock:
            server.stream_info_inflight.pop(key, None)


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
            or stream_info.get('compatibility')
            or not str(stream_info.get("url") or "").startswith(("http://", "https://"))
        ):
            # Expired records are ignored on reads. A successful asynchronous
            # upsert replaces them; a cache miss must never acquire the writer.
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
    *,
    resolved_at: float | None = None,
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
            WHERE recommendation_feature_store.updated_at <= excluded.updated_at
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
                time.time() if resolved_at is None else resolved_at,
            ),
        )
        connection.commit()
    except Exception:
        return
    finally:
        if connection is not None:
            connection.close()


def _delete_persisted_stream_info(server: Any, video_id: str) -> None:
    _defer_stream_store(server, _delete_stream_info_before, video_id, time.time())


def _delete_stream_info_before(server: Any, video_id: str, cutoff: float) -> None:
    from ..recommend.store_runtime import (
        open_recommendation_store_connection_without_init,
    )

    connection = None
    try:
        connection = open_recommendation_store_connection_without_init(server)
        connection.execute(
            """
            DELETE FROM recommendation_feature_store
            WHERE namespace = ? AND entity_id = ? AND updated_at < ?
            """,
            (_STREAM_RESOLUTION_NAMESPACE, video_id, cutoff),
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
    if payload['code'] == 'preparation_busy':
        return payload  # Capacity pressure is not a bad source or a cooldown.
    with server.stream_failure_lock:
        server.stream_failure_cache[video_id] = payload
    return payload


def _clear_stream_failure(server: Any, video_id: str) -> None:
    with server.stream_failure_lock:
        server.stream_failure_cache.pop(video_id, None)


class _QuietYtDlpLogger:
    """Keep fixed failure categories, never provider messages or credentials."""

    def __init__(self):
        self.flags = set()
        self.request_headers_ms = {}
        self.stages_ms = {}

    def debug(self, message: str) -> None:
        self.warning(message)

    def warning(self, message: str) -> None:
        text = str(message).casefold()
        for code, phrases in {
            'signature_failure': ('signature extraction failed', 'signature solving failed'),
            'n_challenge_failure': ('n challenge solving failed', 'nsig extraction failed'),
            'cookies_invalid': ('cookies are no longer valid', 'cookies have been invalidated'),
            'sign_in_required': ('sign in to confirm', 'login required'),
            'token_missing': ('po token is not provided', 'missing a po token'),
            'token_failed': ('failed to fetch po token', 'failed to generate po token'),
        }.items():
            if any(phrase in text for phrase in phrases):
                self.flags.add(code)

    def error(self, message: str) -> None:
        self.warning(message)


def _ytdlp_opts(server: Any) -> dict:
    client = os.environ.get('NEATIE_DIRECT_CLIENT', 'web_music').lower()
    if client not in {'web_music', 'mweb'}:
        client = 'web_music'
    opts = {
        "format": "bestaudio[ext=m4a][protocol=https]/bestaudio[protocol=https]",
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietYtDlpLogger(),
        "socket_timeout": 8,
        "extractor_retries": 1,
        "neatie_fast_acquisition": str(getattr(server, 'NEATIE_FAST_ACQUISITION',
            os.environ.get('NEATIE_FAST_ACQUISITION', '0'))).lower() in {'1', 'true'},
        "extractor_args": {"youtube": {
            "player_client": [client],
            "skip": ["hls", "dash", "translated_subs"],
            "player_skip": ["configs", "initial_data"],
        }},
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
    flags = getattr(exc, '_auralis_extractor_flags', None)
    if flags:
        diagnostics['extractor_flags'] = sorted(flags)
    return diagnostics


@contextmanager
def _new_ytdlp(options, cookie_path, fast, refresh_bootstrap):
    # A mounted master jar is never assigned to YoutubeDL's save-back owner.
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
            if fast:
                from .youtube_acquisition import install_direct_extractor
                install_direct_extractor(ydl, refresh=refresh_bootstrap)
            ydl._neatie_extractor_reused = False
            logger = options.get('logger')
            if isinstance(logger, _QuietYtDlpLogger):
                original_open = ydl.urlopen
                def timed_open(request):
                    logger = ydl.params.get('logger')
                    path = urllib.parse.urlsplit(getattr(request, 'url', '')).path
                    stage = ('token' if path == '/get_pot' else
                             'player' if path.endswith('/player') else
                             'watch' if path == '/watch' else
                             'player_script' if path.endswith('.js') else 'other')
                    started = time.perf_counter()
                    if path == '/get_pot':
                        request.extensions['timeout'] = 8
                    try:
                        return original_open(request)
                    finally:
                        if isinstance(logger, _QuietYtDlpLogger):
                            logger.request_headers_ms[stage] = logger.request_headers_ms.get(stage, 0) + round(
                                (time.perf_counter()-started)*1000)
                ydl.urlopen = timed_open
            yield ydl


@contextmanager
def _ytdlp_extractor(options: dict):
    options = dict(options)
    cookie_path = options.pop('cookiefile', None)
    fast = options.pop('neatie_fast_acquisition', False)
    refresh_bootstrap = options.pop('neatie_refresh_bootstrap', False)
    background = _background_acquisition.get()
    logger = options.get('logger')
    admitted_at = time.perf_counter()
    reserved = background and _direct_background_slots.acquire(blocking=False)
    if background and not reserved:
        raise RuntimeError('audio_resolver_busy')
    admitted = _direct_extraction_slots.acquire(timeout=2 if not background else 0)
    if not admitted:
        if reserved:
            _direct_background_slots.release()
        raise RuntimeError('audio_resolver_busy')
    if isinstance(logger, _QuietYtDlpLogger):
        logger.stages_ms['slot_wait'] = round((time.perf_counter()-admitted_at)*1000)
    try:
        def factory():
            return _new_ytdlp(options, cookie_path, fast, refresh_bootstrap)
        if fast and os.environ.get('NEATIE_DIRECT_POOL', '0') == '1':
            from .youtube_acquisition import reuse_extractor
            manager = reuse_extractor(factory, options, cookie_path, refresh=refresh_bootstrap)
        else:
            manager = factory()
        setup_at = time.perf_counter()
        with manager as ydl:
            if isinstance(logger, _QuietYtDlpLogger):
                logger.stages_ms['setup'] = round((time.perf_counter()-setup_at)*1000)
            yield ydl
    except Exception as exc:
        _mark_failure_stage(exc, "extraction")
        logger = options.get('logger')
        if isinstance(logger, _QuietYtDlpLogger):
            exc._auralis_extractor_flags = tuple(logger.flags)
        raise
    finally:
        _direct_extraction_slots.release()
        if reserved:
            _direct_background_slots.release()


def extract_stream_info(server: Any, video_id: str, refresh_bootstrap=False):
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = _ytdlp_opts(server)
    ydl_opts['neatie_refresh_bootstrap'] = refresh_bootstrap
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
            and str(entry.get("vcodec") or "none").lower() == "none"
            and entry.get('protocol') in {None, '', 'http', 'https'}
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
        "extraction_request_headers_ms": dict(ydl_opts['logger'].request_headers_ms),
        "extraction_stages_ms": dict(ydl_opts['logger'].stages_ms),
        "extractor_flags": sorted(ydl_opts['logger'].flags),
        "acquisition_path": 'direct_fast' if getattr(ydl, '_neatie_fast_acquisition', False) is True else 'direct_standard',
        "session_bootstrap_reused": getattr(ydl, '_neatie_bootstrap_reused', False) is True,
        "extractor_reused": getattr(ydl, '_neatie_extractor_reused', False) is True,
        "direct_client": ydl_opts['extractor_args']['youtube']['player_client'][0],
    }


def validate_stream_info(server: Any, video_id: str, stream_info: dict) -> dict:
    """Prove that an extracted URL yields media before it can be cached."""
    if stream_info.get("protocol") not in {None, "", "http", "https"}:
        raise RuntimeError("Requested format is not available: direct audio required")
    headers = dict(stream_info.get("headers") or {})
    combined = (os.environ.get('NEATIE_COLD_COMBINED_READ', '1') != '0'
                and not stream_info.get('compatibility'))
    retained = combined and not _background_acquisition.get() and os.environ.get('NEATIE_RETAIN_STARTUP_READ', '0') == '1'
    request_last = 1048575 if retained else 65535
    headers['Accept-Encoding'] = 'identity'
    headers["range"] = f'bytes=0-{request_last}' if combined else 'bytes=0-0'
    kept = False
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
        content_type = str(response.headers.get("content-type") or "").casefold()
        if "mpegurl" in content_type or "dash+xml" in content_type:
            raise RuntimeError("Requested format is not available: playlist is not audio")
        if content_type.startswith(("text/", "application/json")):
            raise RuntimeError(f"stream_preflight_invalid_content_type:{content_type}")
        validated = dict(stream_info)
        if content_type:
            validated['mime_type'] = content_type.split(';')[0]
        validated["validated_at"] = time.time()
        validated["validation_video_id"] = video_id
        if combined:
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('content-range', ''))
            if response.status_code != 206 or not match or int(match[1]) != 0:
                raise ValueError('Combined preflight did not honor the byte range')
            total, last = int(match[3]), int(match[2])
            if total <= 0 or last != min(request_last, total-1):
                raise ValueError('Combined preflight returned an invalid audio range')
            declared = response.headers.get('content-length')
            if declared is not None and int(declared) != last+1:
                raise ValueError('Combined preflight returned an invalid content length')
            if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                raise ValueError('Combined preflight changed the byte encoding')
            pieces, received = [], 0
            target = min(65536, total)
            chunks = iter(response.iter_content(chunk_size=65536))
            for chunk in chunks:
                if not chunk:
                    continue
                received += len(chunk)
                if received > target:
                    raise ValueError('Combined preflight exceeded the byte range')
                pieces.append(chunk)
                if retained and received == target:
                    break
            first_chunk = b''.join(pieces)
            if received != target:
                raise ValueError('Combined preflight returned incomplete audio')
            store_stream_chunk(server, video_id, {'bytes': first_chunk,
                'content_type': content_type.split(';')[0], 'total_length': total,
                'representation_key': stream_representation_key(validated),
                'etag': response.headers.get('etag'), 'compatibility': None})
            if retained and last+1 > received:
                _keep_initial_read(server, video_id, validated, response, chunks, received,
                    last, total, content_type.split(';')[0], response.headers.get('etag'))
                kept = True
        else:
            first_chunk = next((chunk for chunk in response.iter_content(chunk_size=1) if chunk), b'')
            if not first_chunk:
                raise RuntimeError('stream_preflight_empty')
        return validated
    except Exception as exc:
        _mark_failure_stage(exc, "media_preflight")
        raise
    finally:
        if not kept:
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
    target = min(65536, target_bytes, int(metadata['content_length']))
    while True:
        existing = get_cached_stream_chunk(server, video_id)
        if existing and existing.get('representation_key') == key and len(existing['bytes']) >= target:
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
            data = b''.join(cache.iter_object(video_id, start=0, end=target-1, expected=metadata))
            if len(data) != target:
                raise ValueError('Invalid cached startup prefix')
            result = {'bytes': data, 'representation_key': key, 'total_length': metadata['content_length'],
                      'content_type': metadata['content_type'], 'etag': metadata['etag']}
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
            try:
                pending.result(timeout=25)
            except Exception:
                pass
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
        if stream_cache.backend_name == 'r2':
            schedule_cached_audio_repair(server, video_id, stream_cache_meta)
        prefix = None
        prefix_started = time.perf_counter()
        if stream_cache.backend_name == 'r2':
            try:
                prefix = warm_cached_object_prefix(server, video_id, stream_cache, stream_cache_meta)
            except Exception:
                pass  # A prefix miss is not proof that startup bytes exist.
        content_length = int(stream_cache_meta.get("content_length") or 0)
        cache_path = ''
        if stream_cache.backend_name == 'r2' and isinstance(stream_cache_meta.get('key'), str):
            variant = 'seek-v1' if stream_cache_meta['key'].endswith('.seek-v1') else 'legacy'
            cache_path = f'/playback/stream/{video_id}?resolve=1&source_id={video_id}&cache_variant={variant}'
        metrics = {
            "prepared": True,
            "playback_path": cache_path,
            "playback_url": "",
            "source_kind": f"{stream_cache.backend_name}_proxy",
            "cache_tier": "r2_full" if stream_cache.backend_name == "r2" else "local_full",
            "headers": {},
            "mime_type": stream_cache_meta.get("content_type") or "audio/mp4",
            "content_length": content_length,
            "range_supported": True,
            # Prefix bytes may expire; this immutable cached resource does not
            # inherit the upstream URL/prefix lease. Availability is checked at
            # read time and sessions still pin key, ETag, type and length.
            "expires_at": time.time() + 12 * 60 * 60,
            "r2_cache_hit": bool(stream_cache.backend_name == "r2"),
            "resolve_cache_hit": False,
            "chunk_cache_hit": prefix is not None,
            "chunk_deferred": prefix is None,
            "resolve_ms": 0,
            "chunk_ms": int((time.perf_counter()-prefix_started)*1000),
            "stream_cache_head_ms": stream_cache_head_ms,
            "target_chunk_bytes": target_chunk_bytes,
            "cached_prefix_bytes": len(prefix['bytes']) if prefix else 0,
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


def prepare_stream_track_safely(server: Any, video_id: str, target_chunk_bytes: int, active_queue: bool):
    try:
        return prepare_stream_track(server, video_id, target_chunk_bytes, active_queue)
    except Exception as exc:
        _store_stream_failure(server, video_id, exc)
        return None


def classify_stream_failure(exc: Exception):
    if str(exc) == 'audio_resolver_busy':
        return {'code':'preparation_busy','message':'Audio preparation capacity is busy',
                'status_code':503}
    if isinstance(exc, TimeoutError):
        return {"code": "preparation_timeout", "message": "Audio preparation timed out",
                "status_code": 504}
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
                # Browser resolution may validly wait 75 seconds, bounded by
                # the resolver's 80-second budget. Do not fail it at 25 seconds.
                prepared[video_id] = future.result(timeout=85)
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
        if entry and entry['expires_at'] > time.time() + (0 if session else 90):
            return entry['payload']
        if entry and session:
            raise PlaybackSourceExpired('Playback source expired; start playback again')
    return None


def get_stream_info(server: Any, video_id: str, *, background=False, session=''):
    selected = selected_stream_info(server, video_id, session)
    if selected is not None:
        if selected.get('compatibility'):
            raise PlaybackSourceExpired('Browser capture has been retired; start playback again')
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
                payload = None
            if payload is not None:
                print(f'[EBB:stream] video={video_id} event=prepared_source_reused')
        if payload is None:
            payload = _resolve_stream_info(server, video_id, background=background,
                                           priority=0 if session else (2 if background else 1))
        payload = {**payload, 'pinned': bool(session)}
        with server.stream_info_lock:
            keys = [k for k in server.stream_info_cache if k.startswith('selected:')]
            for old in keys[:-255]:
                server.stream_info_cache.pop(old, None)
            server.stream_info_cache[key] = {
                'payload': payload,
                'expires_at': float(payload.get('expires_at') or time.time()+1200),
            }
        pending.set_result(payload)
        return payload
    except Exception as exc:
        pending.set_exception(exc)
        raise
    finally:
        with server.stream_info_lock:
            server.stream_info_inflight.pop(key, None)


def _resolve_stream_info(server: Any, video_id: str, *, background=False, priority=0):
    # Playback and speculative preparation share direct acquisition only.
    # Never open an authenticated browser player as a recovery transport.
    token = _background_acquisition.set(background)
    try:
        return _get_primary_stream_info(server, video_id)
    finally:
        _background_acquisition.reset(token)


def _get_primary_stream_info(server: Any, video_id: str):
    now = time.time()
    cached_failure = _get_stream_failure(server, video_id)
    if cached_failure is not None:
        raise RuntimeError(cached_failure.get("message") or cached_failure.get("code") or "stream_failed")
    with server.stream_info_lock:
        cached = server.stream_info_cache.get(video_id)
        if cached and cached["expires_at"] > now + 90:
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

    timings = {}
    def measured(name, fn, *args):
        started = time.perf_counter()
        try:
            return fn(*args)
        finally:
            timings[name] = timings.get(name, 0) + int((time.perf_counter()-started)*1000)
    def fresh_resolution():
        info = measured('extraction', extract_stream_info, server, video_id)
        try:
            return measured('validation', validate_stream_info, server, video_id, info)
        except Exception as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if info.get('acquisition_path') != 'direct_fast' or status not in {401, 403}:
                raise
            # A stale session must not poison the source for fifteen minutes.
            # Retry one fresh context only before any media is published.
            return measured('validation', validate_stream_info, server, video_id,
                measured('extraction', extract_stream_info, server, video_id, True))
    try:
        persisted = measured('persisted_lookup', _load_persisted_stream_info, server, video_id)
        if persisted is not None:
            payload, expires_at = persisted
            try:
                payload = measured('validation', validate_stream_info, server, video_id, payload)
            except Exception:
                _delete_persisted_stream_info(server, video_id)
                payload = fresh_resolution()
                expires_at, should_persist = _resolved_stream_expiry(server, payload)
                payload["expires_at"] = expires_at
                if should_persist:
                    _defer_stream_store(server, _persist_stream_info, video_id, payload, expires_at, resolved_at=now)
        else:
            payload = fresh_resolution()
            expires_at, should_persist = _resolved_stream_expiry(server, payload)
            payload = dict(payload)
            payload["expires_at"] = expires_at
            if should_persist:
                _defer_stream_store(server, _persist_stream_info, video_id, payload, expires_at, resolved_at=now)
        payload = {**payload, 'resolution_timings_ms': timings}
        print(f'[EBB:resolution] track={video_id} extractionMs={timings.get("extraction", 0)} '
              f'validationMs={timings.get("validation", 0)} persistedMs={timings.get("persisted_lookup", 0)} '
              f'client={payload.get("direct_client", "web_music")} poolReused={int(payload.get("extractor_reused") is True)} '
              f'headersMs={json.dumps(payload.get("extraction_request_headers_ms", {}), separators=(",", ":"))} '
              f'stagesMs={json.dumps(payload.get("extraction_stages_ms", {}), separators=(",", ":"))} '
              f'flags={",".join(payload.get("extractor_flags", [])) or "none"}', flush=True)
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
