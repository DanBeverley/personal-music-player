"""Source acquisition, expiring leases and failure policy for playback."""
from __future__ import annotations

import json
import os
import shutil
import time
import urllib.parse
import re
import uuid
from threading import (BoundedSemaphore, Lock, Thread, Event)
from concurrent.futures import (Future)
from typing import (Any)
from contextlib import contextmanager
from contextvars import ContextVar

import yt_dlp
import yt_dlp.plugins
from yt_dlp.cookies import YoutubeDLCookieJar

from .stream_cache import (opus_playback_enabled)


from .stream_buffers import _keep_initial_read, store_stream_chunk, stream_representation_key

_stream_store_slots = BoundedSemaphore(2)
_direct_extraction_slots = BoundedSemaphore(2)
BACKGROUND_EXTRACTION_CAPACITY = 1
_direct_background_slots = BoundedSemaphore(BACKGROUND_EXTRACTION_CAPACITY)
_background_acquisition = ContextVar('background_acquisition', default=False)
_explicit_source_retry = ContextVar('explicit_source_retry', default=False)
_direct_client_override = ContextVar('direct_client_override', default=None)
playback_trace_context = ContextVar('playback_trace_context', default=None)
_startup_tools_lock = Lock()
_startup_tools_thread = None
_startup_tools_stop = Event()
_STREAM_RESOLUTION_NAMESPACE = "playback_stream_resolution"
_STREAM_RESOLUTION_MODEL = "youtube"
_STREAM_RESOLUTION_EXPIRY_MARGIN_SECONDS = 90
# Plugin discovery is serialized by Python's import lock.
yt_dlp.plugins.load_all_plugins()

def start_playback_tools(server):
    """One demand-gated refresh owner, sharing the bounded extractor pool."""
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
            from ..recommend.workload import is_idle
            from .youtube_acquisition import (
                warm_generic_session, generic_session_refresh_due, _context_ttl,
            )
            failures, retry_at = 0, 0.0
            try:
                while not _startup_tools_stop.is_set():
                    if not is_idle(server) and time.monotonic() >= retry_at:
                        started = time.perf_counter()
                        result = None
                        token = _background_acquisition.set(True)
                        try:
                            with _ytdlp_extractor(_ytdlp_opts(server)) as ydl:
                                if not _startup_tools_stop.is_set() and generic_session_refresh_due(ydl):
                                    result = 'ready' if warm_generic_session(ydl) else 'unavailable'
                        except Exception as error:
                            result = 'busy' if str(error) == 'audio_resolver_busy' else type(error).__name__
                        finally:
                            _background_acquisition.reset(token)
                        if result is not None:
                            if result == 'ready':
                                failures, retry_at = 0, 0.0
                            elif result != 'busy':
                                failures = min(failures+1, 5)
                                retry_at = time.monotonic()+min(_context_ttl(), 30*2**failures)
                            print(f'[EBB:playback-tools] result={result} '
                                  f'ms={int((time.perf_counter()-started)*1000)}', flush=True)
                    if _startup_tools_stop.wait(min(30, _context_ttl()/6)):
                        break
            finally:
                from .youtube_acquisition import close_idle_extractors
                close_idle_extractors()
        _startup_tools_thread = Thread(target=work, name='playback-startup', daemon=True)
        _startup_tools_thread.start()
        return True


def stop_playback_tools():
    _startup_tools_stop.set()
    from .youtube_acquisition import close_idle_extractors
    close_idle_extractors()


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


class CachedStreamFailure(RuntimeError):
    """Keep a cached failure's category/stage without renewing its lifetime."""

    def __init__(self, payload):
        self.payload = dict(payload)
        super().__init__(payload['code'])


@contextmanager
def explicit_source_retry(enabled: bool):
    token = _explicit_source_retry.set(enabled)
    try:
        yield
    finally:
        _explicit_source_retry.reset(token)


def _stream_failure_payload(exc: Exception):
    if isinstance(exc, CachedStreamFailure):
        return dict(exc.payload)
    classified = classify_stream_failure(exc)
    return {
        **classified,
        'diagnostics': stream_failure_diagnostics(exc),
        "created_at": time.time(),
    }


def _get_stream_failure(server: Any, video_id: str, *, retry: bool = False):
    now = time.time()
    cooldown = int(getattr(server, "STREAM_FAILURE_COOLDOWN_SECONDS", 900) or 900)
    with server.stream_failure_lock:
        cached = server.stream_failure_cache.get(video_id)
        if cached and cached.get('code') not in {'video_unavailable', 'format_unavailable'}:
            cooldown = min(cooldown, 60)
        if cached and (now - float(cached.get("created_at") or 0.0)) < cooldown:
            # Only an explicit foreground Retry can bypass a temporary failure.
            # Keep the reservation through another failure; repeated taps and
            # speculative work cannot create an extraction retry loop.
            if (retry and cached.get('code') not in {'video_unavailable', 'format_unavailable'}
                    and now >= float(cached.get('retry_after') or 0)):
                cached['retry_after'] = now + 30
                return None
            return dict(cached)
        if cached:
            server.stream_failure_cache.pop(video_id, None)
    return None


def _store_stream_failure(server: Any, video_id: str, exc: Exception) -> dict:
    payload = _stream_failure_payload(exc)
    if isinstance(exc, CachedStreamFailure):
        return payload
    if payload['code'] == 'preparation_busy':
        return payload  # Capacity pressure is not a bad source or a cooldown.
    with server.stream_failure_lock:
        previous = server.stream_failure_cache.get(video_id) or {}
        if previous.get('retry_after'):
            payload['retry_after'] = previous['retry_after']
        if len(server.stream_failure_cache) >= 512 and video_id not in server.stream_failure_cache:
            server.stream_failure_cache.pop(next(iter(server.stream_failure_cache)))
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
        self.request_body_ms = {}
        self.request_counts = {}
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
    client = (_direct_client_override.get()
              or os.environ.get('NEATIE_DIRECT_CLIENT', 'web_music')).lower()
    if client not in {'web_music', 'mweb'}:
        client = 'web_music'
    opts = {
        "format": ("bestaudio[ext=webm][acodec=opus][protocol=https]/" if opus_playback_enabled() else "")
                  + "bestaudio[ext=m4a][protocol=https]/bestaudio[protocol=https]",
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


def record_resolution_metrics(trace, info, *, timings=True, extraction=True):
    """Fixed technical facts only. Substages may overlap (token and solver)."""
    if trace is None:
        return
    for field, key in [('acquisitionPath', 'acquisition_path'), ('directClient', 'direct_client'),
                       ('audioCodec', 'audio_codec'), ('formatId', 'format_id')]:
        value = info.get(key)
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,40}', value):
            trace[field] = value
    if not timings:
        return  # Do not repeat historical extraction as work on a warm request.
    if extraction:
        for field, key in [('extractorReused', 'extractor_reused'), ('bootstrapReused', 'session_bootstrap_reused')]:
            if isinstance(info.get(key), bool):
                trace[field] = info[key]
    for key, names in [
        ('resolution_timings_ms', [('extractMs', 'extraction'), ('validateMs', 'validation'), ('persistedLookupMs', 'persisted_lookup')]),
        ('extraction_stages_ms', [('extractSlotMs', 'slot_wait'), ('extractSetupMs', 'setup'),
            ('initializeMs', 'initialize'), ('bootstrapMs', 'bootstrap'), ('solverMs', 'solver'),
            ('solverCalls', 'solver_calls'), ('solverPreparedCalls', 'solver_prepared'),
            ('solverRawCalls', 'solver_raw'), ('solverPythonMs', 'solver_python')]),
        ('extraction_request_headers_ms', [('tokenHeadersMs', 'token'), ('playerHeadersMs', 'player'),
            ('watchHeadersMs', 'watch'), ('scriptHeadersMs', 'player_script')]),
        ('extraction_request_body_ms', [('tokenBodyMs', 'token'), ('playerBodyMs', 'player'),
            ('watchBodyMs', 'watch'), ('scriptBodyMs', 'player_script')]),
        ('extraction_request_counts', [('tokenRequests', 'token'), ('playerRequests', 'player'), ('watchRequests', 'watch')]),
        ('validation_timings_ms', [('mediaHeadersMs', 'headers'), ('mediaFirstChunkMs', 'first_chunk'),
            ('mediaPrefixMs', 'prefix'), ('mediaPrefixBytes', 'bytes'), ('mediaStatus', 'status'),
            ('mediaTotalBytes', 'total_bytes')]),
    ]:
        if not extraction and key.startswith('extraction_'):
            continue
        for field, source in names:
            value = (info.get(key) or {}).get(source)
            if isinstance(value, (int, float)) and 0 <= value < 1_000_000_000:
                trace[field] = value
    if isinstance(info.get('startup_read_retained'), bool):
        trace['startupReadRetained'] = info['startup_read_retained']


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
                        response = original_open(request)
                        read = response.read
                        def timed_read(*args, **kwargs):
                            reading = time.perf_counter()
                            try:
                                return read(*args, **kwargs)
                            finally:
                                if isinstance(logger, _QuietYtDlpLogger):
                                    logger.request_body_ms[stage] = logger.request_body_ms.get(stage, 0) + round(
                                        (time.perf_counter()-reading)*1000, 1)
                        try:
                            response.read = timed_read
                        except (AttributeError, TypeError):
                            pass  # Optional timing must not change a transport.
                        return response
                    finally:
                        if isinstance(logger, _QuietYtDlpLogger):
                            logger.request_counts[stage] = logger.request_counts.get(stage, 0) + 1
                            logger.request_headers_ms[stage] = logger.request_headers_ms.get(stage, 0) + round(
                                (time.perf_counter()-started)*1000)
                ydl.urlopen = timed_open
            yield ydl


@contextmanager
def _ytdlp_extractor(options: dict):
    options = dict(options)
    configured_cookies = options.pop('cookiefile', None)
    from .youtube_acquisition import cookie_session, reject_cookie_session
    cookie_path, cookie_fingerprint = cookie_session(configured_cookies)
    trace = playback_trace_context.get()
    if trace is not None:
        trace['cookieSessionState'] = ('rejected' if configured_cookies and cookie_path is None
                                       else 'loaded' if cookie_path else 'anonymous')
        clients = options.get('extractor_args', {}).get('youtube', {}).get('player_client', [])
        if clients and clients[0] in {'mweb', 'web_music'}:
            trace['directClient'] = clients[0]
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
            from ..memory_runtime import memory_operation
            ydl._neatie_resolution_id = (trace or {}).get('resolutionId')
            with memory_operation('extractor', correlation=ydl._neatie_resolution_id):
                yield ydl
            if 'cookies_invalid' in getattr(logger, 'flags', set()):
                reject_cookie_session(cookie_fingerprint)
    except Exception as exc:
        _mark_failure_stage(exc, "extraction")
        logger = options.get('logger')
        if isinstance(logger, _QuietYtDlpLogger):
            if 'cookies_invalid' in logger.flags:
                reject_cookie_session(cookie_fingerprint)
            exc._auralis_extractor_flags = tuple(logger.flags)
            trace = playback_trace_context.get()
            record_resolution_metrics(trace, {'extraction_stages_ms': logger.stages_ms,
                'extraction_request_headers_ms': logger.request_headers_ms,
                'extraction_request_body_ms': logger.request_body_ms,
                'extraction_request_counts': logger.request_counts})
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
                (2 if opus_playback_enabled() and entry.get('acodec') == 'opus'
                 and entry.get('ext') == 'webm' else
                 1 if str(entry.get("ext") or "").lower() in {"m4a", "mp4"} else 0),
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
        "extraction_request_body_ms": dict(ydl_opts['logger'].request_body_ms),
        "extraction_request_counts": dict(ydl_opts['logger'].request_counts),
        "extraction_stages_ms": dict(ydl_opts['logger'].stages_ms),
        "extractor_flags": sorted(ydl_opts['logger'].flags),
        "acquisition_path": 'direct_fast' if getattr(ydl, '_neatie_fast_acquisition', False) is True else 'direct_standard',
        "session_bootstrap_reused": getattr(ydl, '_neatie_bootstrap_reused', False) is True,
        "extractor_reused": getattr(ydl, '_neatie_extractor_reused', False) is True,
        "direct_client": ydl_opts['extractor_args']['youtube']['player_client'][0],
        "format_id": str(selected.get('format_id') or ''),
        "audio_codec": str(selected.get('acodec') or ''),
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
    started = time.perf_counter()
    metrics = {}
    try:
        response = server.upstream_http.get(
            stream_info["url"],
            headers=headers,
            stream=True,
            timeout=(5, 20),
        )
    except Exception as exc:
        metrics['headers'] = round((time.perf_counter()-started)*1000, 1)
        record_resolution_metrics(playback_trace_context.get(), {'validation_timings_ms': metrics})
        _mark_failure_stage(exc, "media_preflight")
        raise
    metrics.update(headers=round((time.perf_counter()-started)*1000, 1), status=response.status_code)
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
            metrics['total_bytes'] = total
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
                metrics.setdefault('first_chunk', round((time.perf_counter()-started)*1000, 1))
                received += len(chunk)
                if received > target:
                    raise ValueError('Combined preflight exceeded the byte range')
                pieces.append(chunk)
                if retained and received == target:
                    break
            first_chunk = b''.join(pieces)
            if received != target:
                raise ValueError('Combined preflight returned incomplete audio')
            metrics.update(prefix=round((time.perf_counter()-started)*1000, 1), bytes=received)
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
            metrics.update(first_chunk=round((time.perf_counter()-started)*1000, 1), bytes=len(first_chunk))
        validated['validation_timings_ms'] = dict(metrics)
        validated['startup_read_retained'] = kept
        return validated
    except Exception as exc:
        _mark_failure_stage(exc, "media_preflight")
        raise
    finally:
        record_resolution_metrics(playback_trace_context.get(), {'validation_timings_ms': metrics,
            'startup_read_retained': kept})
        if not kept:
            response.close()


def is_stream_info_cached(server: Any, video_id: str):
    now = time.time()
    with server.stream_info_lock:
        cached = server.stream_info_cache.get(video_id)
        return bool(cached and cached["expires_at"] > now)


def classify_stream_failure(exc: Exception):
    if isinstance(exc, CachedStreamFailure):
        return {key: exc.payload[key] for key in ('code', 'message', 'status_code', 'diagnostics')
                if key in exc.payload}
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
            "message": "This recording is unavailable",
            "status_code": 410,
        }
    if "requested format is not available" in lowered:
        return {
            "code": "format_unavailable",
            "message": "No supported audio format is available",
            "status_code": 410,
        }
    if (
        status_code in {401, 403, 429}
        or "sign in to confirm" in lowered
        or "not a bot" in lowered
        or "source_blocked" in lowered
        or set(getattr(exc, '_auralis_extractor_flags', ())) & {'cookies_invalid', 'sign_in_required'}
    ):
        return {
            "code": "source_blocked",
            "message": "The music provider requires verification",
            "status_code": 502,
        }
    return {
        "code": "stream_failed",
        "message": "The audio source could not be retrieved",
        "status_code": 500,
    }


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


def _remember_selected_stream(server, video_id, session, payload, expires_at):
    with server.stream_info_lock:
        keys = [key for key in server.stream_info_cache if key.startswith('selected:')]
        for key in keys[:-254]:
            server.stream_info_cache.pop(key, None)
        server.stream_info_cache[f'selected:{session}:{video_id}'] = {
            'payload': payload, 'expires_at': expires_at,
        }


def get_stream_info(server: Any, video_id: str, *, background=False, session=''):
    request_trace = playback_trace_context.get()
    selected = selected_stream_info(server, video_id, session)
    if selected is not None:
        if selected.get('compatibility'):
            raise PlaybackSourceExpired('Browser capture has been retired; start playback again')
        if request_trace is not None:
            request_trace['resolutionOrigin'] = 'pinned' if session else 'memory'
        return selected
    key = f'selected:{session}:{video_id}'
    with server.stream_info_lock:
        pending = server.stream_info_inflight.get(key)
        owner = pending is None
        if owner:
            pending = Future()
            server.stream_info_inflight[key] = pending
    if not owner:
        started = time.perf_counter()
        if request_trace is not None:
            request_trace['resolutionOrigin'] = 'shared'
        try:
            return pending.result(timeout=85)
        finally:
            if request_trace is not None:
                request_trace['sharedResolutionWaitMs'] = round((time.perf_counter()-started)*1000, 1)
    try:
        payload = selected_stream_info(server, video_id, session)
        if (payload is None and session and not (
                _explicit_source_retry.get() and video_id in server.stream_failure_cache)):
            payload = selected_stream_info(server, video_id)
            if payload is not None and payload.get('compatibility'):
                payload = None
            if payload is not None:
                print(f'[EBB:stream] video={video_id} event=prepared_source_reused')
        if payload is not None and request_trace is not None:
            request_trace['resolutionOrigin'] = 'prepared' if session else 'memory'
        if payload is None:
            payload = _resolve_stream_info(server, video_id, background=background)
        payload = {**payload, 'pinned': bool(session)}
        _remember_selected_stream(server, video_id, session, payload,
                                  float(payload.get('expires_at') or time.time()+1200))
        pending.set_result(payload)
        return payload
    except Exception as exc:
        pending.set_exception(exc)
        raise
    finally:
        with server.stream_info_lock:
            server.stream_info_inflight.pop(key, None)


def _resolve_stream_info(server: Any, video_id: str, *, background=False):
    # Playback and speculative preparation share direct acquisition only.
    # Never open an authenticated browser player as a recovery transport.
    token = _background_acquisition.set(background)
    try:
        return _get_primary_stream_info(server, video_id)
    finally:
        _background_acquisition.reset(token)


def _get_primary_stream_info(server: Any, video_id: str):
    now = time.time()
    request_trace = playback_trace_context.get()
    with server.stream_info_lock:
        pending = server.stream_info_inflight.get(video_id)
        if pending is None:
            retrying = (_explicit_source_retry.get() and not _background_acquisition.get()
                        and video_id in server.stream_failure_cache)
            cached_failure = _get_stream_failure(server, video_id,
                retry=retrying)
            if cached_failure is not None:
                if request_trace is not None:
                    request_trace.update(resolutionOrigin='failure_cache')
                raise CachedStreamFailure(cached_failure)
            cached = server.stream_info_cache.get(video_id)
            if cached and cached["expires_at"] > now + 90 and not retrying:
                if request_trace is not None:
                    request_trace.update(resolutionOrigin='memory')
                return cached["payload"]
            pending = Future()
            server.stream_info_inflight[video_id] = pending
            should_extract = True
        else:
            should_extract = False

    if not should_extract:
        started = time.perf_counter()
        if request_trace is not None:
            request_trace['resolutionOrigin'] = 'shared'
        try:
            return pending.result(timeout=25)
        finally:
            if request_trace is not None:
                request_trace['sharedResolutionWaitMs'] = round((time.perf_counter()-started)*1000, 1)

    timings = {}
    resolution_started = time.perf_counter()
    resolution_id = uuid.uuid4().hex[:12]
    if request_trace is not None:
        request_trace['resolutionId'] = resolution_id
    def measured(name, fn, *args):
        started = time.perf_counter()
        try:
            return fn(*args)
        finally:
            timings[name] = timings.get(name, 0) + int((time.perf_counter()-started)*1000)
    def fresh_resolution():
        retry_client = None
        for attempt in range(2):
            if request_trace is not None:
                request_trace['resolutionAttempts'] = attempt+1
            info = None
            validation = None
            before = dict(timings)
            attempt_started = time.perf_counter()
            observed = {'resolutionId': resolution_id} if request_trace is not None else None
            observed_context = playback_trace_context.set(observed)
            outcome = {'result': 'failed'}
            client_token = _direct_client_override.set(retry_client)
            try:
                # Rejection invalidates that client's context, not a separately
                # keyed alternate client's still-valid generic setup.
                refresh = retrying or (attempt and retry_client is None)
                info = (measured('extraction', extract_stream_info, server, video_id, True)
                        if refresh else measured('extraction', extract_stream_info, server, video_id))
                record_resolution_metrics(observed, info)
                validation = measured('validation', validate_stream_info, server, video_id, info)
                outcome = {'result': 'validated'}
                return validation
            except Exception as exc:
                facts = stream_failure_diagnostics(exc)
                outcome.update(failureStage=facts['stage'], exceptionType=facts['exception_type'])
                if 'http_status' in facts:
                    outcome['failureHttpStatus'] = facts['http_status']
                if facts.get('extractor_flags'):
                    outcome['failureFlags'] = ','.join(facts['extractor_flags'])
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
                flags = getattr(exc, '_auralis_extractor_flags', ())
                invalid_auth = 'cookies_invalid' in flags
                if invalid_auth and request_trace is not None:
                    request_trace['providerSessionInvalid'] = True
                expired_context = (info or {}).get('acquisition_path') == 'direct_fast' and status in {401, 403}
                if attempt or not (invalid_auth or expired_context and not retrying):
                    raise
                if expired_context:
                    # A new bootstrap of the same client still returns denied
                    # media for some sources. Use the other direct client in
                    # the existing, pre-publication retry; never add a third.
                    client = info.get('direct_client') or _ytdlp_opts(server)['extractor_args']['youtube']['player_client'][0]
                    retry_client = 'web_music' if client == 'mweb' else 'mweb'
                if request_trace is not None:
                    request_trace['freshRetryReason'] = 'invalid_session' if invalid_auth else 'media_denied'
                    if retry_client:
                        request_trace['freshRetryClient'] = retry_client
                # One fresh attempt before publication. An explicitly rejected
                # jar is quarantined in memory until its file contents change.
            finally:
                _direct_client_override.reset(client_token)
                playback_trace_context.reset(observed_context)
                if request_trace is not None:
                    record_resolution_metrics(observed, validation or info or {})
                    request_trace.update(observed)
                    row = {**observed, **outcome, 'sourceId': video_id,
                        'resolutionId': resolution_id, 'attempt': attempt+1,
                        'startMs': round((attempt_started-resolution_started)*1000, 1),
                        'totalMs': round((time.perf_counter()-attempt_started)*1000, 1),
                        'extractMs': timings.get('extraction', 0)-before.get('extraction', 0),
                        'validateMs': timings.get('validation', 0)-before.get('validation', 0)}
                    duration = (info or {}).get('duration')
                    if isinstance(duration, (int, float)) and 0 < duration < 86400:
                        row['sourceDuration'] = duration
                    rows = request_trace.setdefault('acquisitionAttempts', [])
                    if len(rows) < 6:
                        rows.append(row)
                    else:
                        request_trace['acquisitionAttemptsDropped'] = request_trace.get('acquisitionAttemptsDropped', 0)+1
    try:
        persisted = None if retrying else measured('persisted_lookup', _load_persisted_stream_info, server, video_id)
        if request_trace is not None:
            request_trace.update(persistedUrlAtStart=persisted is not None,
                                 resolutionOrigin='persisted' if persisted is not None else 'fresh')
        if persisted is not None:
            payload, expires_at = persisted
            try:
                payload = measured('validation', validate_stream_info, server, video_id, payload)
            except Exception:
                if request_trace is not None:
                    request_trace['resolutionOrigin'] = 'persisted_refresh'
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
        payload = {**payload, 'resolution_timings_ms': timings, 'resolutionId': resolution_id}
        print(f'[EBB:resolution] track={video_id} resolution={resolution_id} extractionMs={timings.get("extraction", 0)} '
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
        failed = _store_stream_failure(server, video_id, exc)
        facts = failed.get('diagnostics') or {}
        print(f'[EBB:resolution-failed] track={video_id} resolution={resolution_id} code={failed["code"]} '
              f'stage={facts.get("stage", "source_resolution")} '
              f'error={facts.get("exception_type", type(exc).__name__)} '
              f'httpStatus={facts.get("http_status", 0)} '
              f'flags={",".join(facts.get("extractor_flags", [])) or "none"} '
              f'extractionMs={timings.get("extraction", 0)} '
              f'validationMs={timings.get("validation", 0)}', flush=True)
        pending.set_exception(exc)
        raise
    finally:
        if request_trace is not None:
            request_trace.update(extractMs=timings.get('extraction', 0),
                                 validateMs=timings.get('validation', 0),
                                 persistedLookupMs=timings.get('persisted_lookup', 0))
            attempts = request_trace.setdefault('resolutions', [])
            if len(attempts) < 3:
                attempts.append({'resolutionId': resolution_id, 'sourceId': video_id,
                    'extractMs': timings.get('extraction', 0),
                    'validateMs': timings.get('validation', 0),
                    'persistedLookupMs': timings.get('persisted_lookup', 0),
                    **{k: request_trace[k] for k in ('resolutionOrigin', 'resolutionAttempts',
                        'freshRetryReason', 'freshRetryClient', 'directClient',
                        'persistedUrlAtStart') if k in request_trace}})
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
