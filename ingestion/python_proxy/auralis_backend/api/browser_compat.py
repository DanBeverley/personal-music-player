"""Deployment-opt-in transport. Primary extraction remains the default."""
import os
import re
import time
import requests
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor, TimeoutError, wait, FIRST_COMPLETED
from threading import BoundedSemaphore

_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix='audio-resolve')
_capacity = BoundedSemaphore(4)


def _submit(work):
    if not _capacity.acquire(blocking=False):
        return None
    try:
        future = _workers.submit(work)
    except BaseException:
        _capacity.release()
        raise
    future.add_done_callback(lambda _: _capacity.release())
    return future


def race_primary(primary, alternate, delay=1.0):
    """Bounded hedge: return the first validated source, never queue extra work."""
    first = _submit(primary)
    if first is None:
        try:
            return primary()
        except Exception:
            return alternate()
    deadline = time.monotonic() + 80
    error = None
    try:
        return first.result(timeout=delay)
    except TimeoutError:
        pass
    except Exception as exc:
        error = exc
    second = _submit(alternate)
    if second is None:
        return first.result(timeout=80)
    pending = {first, second}
    try:
        while pending:
            done, pending = wait(pending, timeout=max(0, deadline-time.monotonic()),
                                 return_when=FIRST_COMPLETED)
            if not done:
                raise TimeoutError('Audio resolution deadline exceeded')
            for future in done:
                try:
                    result = future.result()
                    if result is not None:
                        return result
                except Exception as exc:
                    error = exc
        raise error or RuntimeError('No playable audio source')
    finally:
        for future in pending:
            future.cancel()

def resolve_browser_audio(video_id, failure_code, *, background=False, priority=0):
    base = os.environ.get('AURALIS_BROWSER_AUDIO_URL', '').strip().rstrip('/')
    if not base or failure_code not in {'source_blocked', 'format_unavailable', 'slow_primary'}:
        return None
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        return None
    # Join the existing capture through slow starts; keep a bounded deadline.
    suffix = '?background=1' if background else (f'?priority={priority}' if priority else '')
    response = requests.post(f'{base}/resolve/{video_id}{suffix}', timeout=(3, 80))
    response.raise_for_status()
    info = response.json()
    if info.get('mime_type') not in {'audio/webm', 'audio/mp4'} or not info.get('duration'):
        raise RuntimeError('Invalid browser audio metadata')
    query = {'background': '1'} if background else ({'priority': str(priority)} if priority else {})
    if info.get('capture'):
        query['capture'] = info['capture']
    stream_suffix = '?' + urlencode(query) if query else ''
    return {'url': f'{base}/stream/{video_id}{stream_suffix}', 'headers': {},
            'mime_type': info['mime_type'], 'duration': info['duration'],
            'expires_at': time.time()+1800, 'compatibility': 'browser_audio'}
