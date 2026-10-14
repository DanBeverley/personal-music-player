"""Bounded acceleration of the installed yt-dlp extractor, not another resolver.

Only session configuration/attestation and public player code are reusable.
Track responses, signatures, tokens and media remain specific to each request.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from contextlib import contextmanager
from pathlib import Path

import yt_dlp.version
from yt_dlp.extractor.youtube import YoutubeIE

_contexts: dict[str, tuple[float, str]] = {}
_context_lock = Lock()
_CONTEXT_TTL = 180
_extractor_pool = []
_extractor_pool_lock = Lock()


def _context_ttl():
    try:
        return min(900, max(30, int(os.environ.get('NEATIE_DIRECT_CONTEXT_TTL', _CONTEXT_TTL))))
    except ValueError:
        return _CONTEXT_TTL


def _close_extractor(entry):
    try:
        entry['manager'].__exit__(None, None, None)
    except Exception:
        pass


def close_idle_extractors():
    with _extractor_pool_lock:
        retired = list(_extractor_pool)
        _extractor_pool.clear()
    for entry in retired:
        _close_extractor(entry)


@contextmanager
def reuse_extractor(factory, options, cookie_path, *, refresh=False):
    """Exclusive leases for at most two idle extractors; no track response cache."""
    stable = {k: v for k, v in options.items() if k != 'logger'}
    try:
        credential = Path(cookie_path).read_bytes() if cookie_path else b''
    except OSError as error:
        error._auralis_failure_stage = 'cookie_load'
        raise
    key = hashlib.sha256(json.dumps([stable, yt_dlp.version.__version__], sort_keys=True, default=str).encode()+credential).hexdigest()
    now = time.monotonic()
    entry, retired = None, []
    with _extractor_pool_lock:
        for candidate in list(_extractor_pool):
            if refresh or candidate['key'] != key or now-candidate['idle_at'] >= 900:
                _extractor_pool.remove(candidate)
                retired.append(candidate)
        if _extractor_pool:
            entry = _extractor_pool.pop()
    for old in retired:
        _close_extractor(old)
    reused = entry is not None
    if entry is None:
        manager = factory()
        entry = {'key': key, 'manager': manager, 'ydl': manager.__enter__()}
    ydl = entry['ydl']
    ydl.params['logger'] = options.get('logger')
    ydl._neatie_bootstrap_reused = False
    ydl._neatie_extractor_reused = reused
    try:
        yield ydl
    except BaseException:
        _close_extractor(entry)
        raise
    else:
        if getattr(ydl, '_neatie_fast_acquisition', False) is not True:
            _close_extractor(entry)
            return
        ydl.params['logger'] = None
        entry['idle_at'] = time.monotonic()
        with _extractor_pool_lock:
            _extractor_pool.append(entry)
            retired = _extractor_pool[:-2]
            del _extractor_pool[:-2]
        for old in retired:
            _close_extractor(old)


_MAX_CONTEXT_BYTES = 512 * 1024
_CONFIG_KEYS = {
    'INNERTUBE_API_KEY', 'INNERTUBE_CONTEXT', 'INNERTUBE_CLIENT_NAME',
    'INNERTUBE_CLIENT_VERSION', 'INNERTUBE_CONTEXT_CLIENT_NAME',
    'INNERTUBE_CONTEXT_CLIENT_VERSION', 'PLAYER_JS_URL', 'SESSION_INDEX',
    'VISITOR_DATA', 'DATASYNC_ID', 'LOGGED_IN', 'DELEGATED_SESSION_ID',
}


def _session_key(ydl) -> str:
    # Credential scope is never logged or persisted. Cookie changes create a
    # different context even when a read-only cookie file keeps the same path.
    cookies = sorted((c.domain, c.path, c.name, c.value) for c in ydl.cookiejar)
    provider = ydl.params.get('extractor_args', {}).get('youtubepot-bgutilhttp', {})
    client = ydl.params.get('extractor_args', {}).get('youtube', {})
    body = json.dumps([cookies, provider, client], sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(body.encode()).hexdigest()


def _session_page(ie, video_id: str, page: str) -> str | None:
    if not page:
        return None
    config = ie.extract_ytcfg(video_id, page) or {}
    kept = {k: v for k, v in config.items() if k in _CONFIG_KEYS}
    # Keep only public script locations from player configuration, never
    # cached playback parameters or another video's initial response.
    contexts = config.get('WEB_PLAYER_CONTEXT_CONFIGS') or {}
    kept['WEB_PLAYER_CONTEXT_CONFIGS'] = {
        k: {'jsUrl': v['jsUrl']} for k, v in contexts.items()
        if isinstance(v, dict) and isinstance(v.get('jsUrl'), str)
    }
    match = re.search(r'(?s)window\s*\.\s*ytAtN\s*\(\s*\{.+?\}\s*\)\s*;', page)
    if not match:
        match = re.search(
            r'''(?sx)window\.ytAtR\s*=\s*(?P<q>['"])(?:\\.|(?!(?P=q)).)*(?P=q)\s*;''', page)
    if not match:
        return None
    result = '<script>ytcfg.set('+json.dumps(kept)+');'+match[0]+'</script>'
    return result if len(result.encode()) <= _MAX_CONTEXT_BYTES else None


def invalidate_context(key: str) -> None:
    with _context_lock:
        _contexts.pop(key, None)


def _record_stage(ydl, name, started):
    logger = ydl.params.get('logger')
    timings = getattr(logger, 'stages_ms', None)
    if timings is not None:
        timings[name] = timings.get(name, 0) + round((time.perf_counter()-started)*1000)


def warm_generic_session(ydl):
    """Prepare generic session/player tools only: no track, token or audio."""
    if getattr(ydl, '_neatie_fast_acquisition', False) is not True:
        return False
    ie = ydl.get_info_extractor('Youtube')
    ie.initialize()
    page = ie._download_webpage('https://www.youtube.com/', None, fatal=False)
    config = ie.extract_ytcfg(None, page or '') or {}
    reusable = _session_page(ie, None, page)
    player_url = ie._extract_player_url(config, webpage=page)
    if not reusable or not player_url:
        return False
    provider = next((p for p in ie._jsc_director.providers.values()
                     if p.PROVIDER_NAME == 'node' and p.is_available()), None)
    if provider is None:
        return False
    player = ie.cache.load(provider._CACHE_SECTION, 'player:'+player_url)
    if not player:
        source = provider._get_player(None, player_url)
        stdin = provider._construct_stdin(source, False, [])
        runtime = provider.runtime_info
        permission = '--permission' if runtime.version_tuple >= (23, 5, 0) else '--experimental-permission'
        completed = subprocess.run([runtime.path, permission, '--no-warnings=ExperimentalWarning', '-'],
            input=stdin, text=True, capture_output=True, timeout=8, check=True)
        output = json.loads(completed.stdout)
        player = output.get('preprocessed_player')
        if output.get('type') == 'error' or not isinstance(player, str) or not player:
            return False
        ie.cache.store(provider._CACHE_SECTION, 'player:'+player_url, player)
    # Publish only after preprocessing succeeds. The normal extractor still
    # resolves per-track responses, signatures and tokens on every request.
    with _context_lock:
        while len(_contexts) >= 2 and ie._neatie_scope not in _contexts:
            _contexts.pop(next(iter(_contexts)))
        _contexts[ie._neatie_scope] = (time.monotonic()+_context_ttl(), reusable)
    return True


class _DirectMusicIE(YoutubeIE):
    @classmethod
    def ie_key(cls):
        return YoutubeIE.ie_key()

    def _real_initialize(self):
        started = time.perf_counter()
        try:
            super()._real_initialize()
            for provider in self._jsc_director.providers.values():
                if provider.PROVIDER_NAME != 'node':
                    continue
                run = provider._run_js_runtime
                def measured_run(stdin, _run=run):
                    at = time.perf_counter()
                    try:
                        return _run(stdin)
                    finally:
                        _record_stage(self._downloader, 'solver', at)
                provider._run_js_runtime = measured_run
        finally:
            _record_stage(self._downloader, 'initialize', started)

    def _download_initial_webpage(self, url, client, video_id):
        key = self._neatie_scope
        with _context_lock:
            now = time.monotonic()
            for old, (until, _) in list(_contexts.items()):
                if until <= now:
                    _contexts.pop(old, None)
            cached = _contexts.get(key)
        if cached is not None:
            self._downloader._neatie_bootstrap_reused = True
            return cached[1]
        page = super()._download_initial_webpage(url, client, video_id)
        reusable = _session_page(self, video_id, page)
        if reusable:
            with _context_lock:
                while len(_contexts) >= 2:
                    _contexts.pop(next(iter(_contexts)))
                _contexts[key] = (time.monotonic()+_context_ttl(), reusable)
        return page

    def _extract_formats_and_subtitles(self, video_id, player_responses, player_url,
                                      live_status, duration):
        # The upstream extractor fetches a GVS token before solving signatures.
        # A private placeholder lets its unchanged solver run concurrently.
        # Replace it before yielding any format; it never reaches media I/O.
        from yt_dlp.extractor.youtube._video import STREAMING_DATA_FETCH_GVS_PO_TOKEN
        originals, jobs = [], {}
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='youtube-token') as executor:
            for response in player_responses:
                streaming = response.get('streamingData') or {}
                fetch = streaming.get(STREAMING_DATA_FETCH_GVS_PO_TOKEN)
                if not callable(fetch):
                    continue
                marker = 'neatie-pending-'+uuid.uuid4().hex
                jobs[marker] = executor.submit(fetch, required=True)
                originals.append((streaming, fetch))
                streaming[STREAMING_DATA_FETCH_GVS_PO_TOKEN] = lambda required=False, value=marker: value
            try:
                for item in super()._extract_formats_and_subtitles(
                        video_id, player_responses, player_url, live_status, duration):
                    if not isinstance(item, dict) or not item.get('url'):
                        yield item
                        continue
                    item = dict(item)
                    for marker, pending in jobs.items():
                        if marker not in item['url']:
                            continue
                        token = pending.result(timeout=12)
                        if not token:
                            item = None
                            break
                        item['url'] = item['url'].replace(marker, urllib.parse.quote(token, safe=''))
                    if item is not None:
                        if 'neatie-pending-' in item['url']:
                            raise RuntimeError('acquisition_token_unresolved')
                        yield item
            finally:
                for streaming, fetch in originals:
                    streaming[STREAMING_DATA_FETCH_GVS_PO_TOKEN] = fetch
                for job in jobs.values():
                    job.cancel()


def install_direct_extractor(ydl, *, refresh=False) -> bool:
    # These are narrowly scoped upstream extension points. A library upgrade
    # must pass the compatibility tests; otherwise keep normal direct yt-dlp.
    if yt_dlp.version.__version__ != '2026.08.19':
        return False
    try:
        import yt_dlp_ejs
        from yt_dlp.extractor.youtube.jsc._builtin.node import NodeJCP
        if yt_dlp_ejs.version != '0.8.0' or not hasattr(NodeJCP, '_ENABLE_PREPROCESSED_PLAYER_CACHE'):
            return False
    except ImportError:
        return False
    NodeJCP._ENABLE_PREPROCESSED_PLAYER_CACHE = True
    extractor = _DirectMusicIE(ydl)
    extractor._neatie_scope = _session_key(ydl)
    if refresh:
        invalidate_context(extractor._neatie_scope)
    ydl.add_info_extractor(extractor)
    ydl._neatie_fast_acquisition = True
    ydl._neatie_bootstrap_reused = False
    return True
