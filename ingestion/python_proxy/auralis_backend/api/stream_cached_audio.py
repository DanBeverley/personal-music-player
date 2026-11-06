"""Bounded validation and codec maintenance of already stored audio."""
from __future__ import annotations

import json
import time
from threading import (BoundedSemaphore)
from concurrent.futures import (Future)


from .stream_cache import (get_stream_cache, cached_resource_version, cached_variant, current_audio_index, indexed_content_type, opus_playback_enabled, aac_playback_enabled, prepare_opus_file)


from .stream_buffers import (cached_object_key, prepared_cached_object, warm_cached_object_prefix)
_cached_repair_slot = BoundedSemaphore(1)

def _read_cached_audio(cache, video_id, head, path, *, streams=False):
    """Read exactly the pinned object once and probe it for either codec path."""
    import subprocess
    expected = int(head['content_length'])
    received = 0
    with open(path, 'wb') as output:
        for chunk in cache.iter_object(video_id, expected=head):
            received += len(chunk)
            if received > expected:
                raise ValueError('Cached audio exceeded its pinned length')
            output.write(chunk)
    if received != expected:
        raise ValueError('Cached audio ended before its pinned length')
    entries = 'format=duration:stream=codec_name,codec_type' if streams else 'format=duration'
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', entries,
        '-of', 'json', path], capture_output=True, timeout=30)
    if probe.returncode:
        raise ValueError('Cached audio probe failed')
    return json.loads(probe.stdout)


def validate_cached_track(server, video_id):
    if not _cached_repair_slot.acquire(blocking=False):
        return False
    try:
        return _validate_cached_track(server, video_id)
    finally:
        _cached_repair_slot.release()


def _cache_proof_key(video_id, head):
    target = 'opus-v1' if opus_playback_enabled() else 'v2'
    return f"validated-cache:{target}:{video_id}:{head.get('etag', '')}"


def _needs_cached_repair(head):
    return not current_audio_index(head) or (
        opus_playback_enabled() and cached_variant(head) not in {'opus-v1', 'aac-v1'})


def _validate_cached_track(server, video_id):
    """Check old full objects without replacing bytes used by active players."""
    import tempfile
    import subprocess
    from pathlib import Path
    from .stream_cache import validate_capture_file
    cache = get_stream_cache(server)
    head = cache.head(video_id)
    if not head:
        return False
    if not _needs_cached_repair(head):
        return True
    proof_key = _cache_proof_key(video_id, head)
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
            facts = _read_cached_audio(cache, video_id, head, path)
            duration = float(facts['format']['duration'])
            if cache.backend_name != 'r2':
                validate_capture_file(path, duration, head['content_type'], remux=False)
                return True
            if opus_playback_enabled():
                normalized, tags = prepare_opus_file(path)
                tags.update(parent_etag=head.get('etag', ''), parent_variant=cached_variant(head))
            else:
                normalized = validate_capture_file(path, duration, head['content_type'])
                tags = {'validated': 'capture-v1', 'seek_index': 'v2', 'duration': str(duration)}
            if (head.get('metadata') or {}).get('source_sha256'):
                tags['source_sha256'] = head['metadata']['source_sha256']
            # The derivative key is immutable; selected sessions retain their
            # original key/ETag/length. Preparation/new sessions pick this copy.
            if not cache.put_file(video_id, normalized, content_type=indexed_content_type(normalized, head['content_type']),
                                  metadata=tags):
                return False
            if hasattr(server, 'stream_chunk_lock'):
                with server.stream_chunk_lock:
                    prefix = server.stream_chunk_cache.get(video_id)
                    if prefix and prefix.get('payload', {}).get('representation_key') == cached_object_key(head):
                        server.stream_chunk_cache.pop(video_id, None)
            if head.get('etag'):
                with server.stream_info_lock:
                    keys = [k for k in server.stream_info_cache if k.startswith('validated-cache:')]
                    for key in keys[:-127]:
                        server.stream_info_cache.pop(key, None)
                    server.stream_info_cache[proof_key] = {'expires_at': time.time()+3600, 'payload': {'valid':True}}
            # The next prepared selection must have real startup bytes for the
            # new representation, not the AAC prefix retired above.
            if opus_playback_enabled() and hasattr(server, 'stream_chunk_inflight_lock'):
                try:
                    selected = cache.head(video_id)
                    if selected and cached_variant(selected) == 'opus-v1' and current_audio_index(selected):
                        warm_cached_object_prefix(server, video_id, cache, selected)
                except Exception:
                    pass  # Valid stored audio remains usable if warming fails.
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


def validate_cached_aac_track(server, video_id):
    """Build a separately pinned AAC resource through the existing warm owner."""
    import tempfile
    import subprocess
    from pathlib import Path
    from .mp4_audio import rechunk_aac
    from .stream_cache import validate_capture_file
    if not aac_playback_enabled() or not _cached_repair_slot.acquire(blocking=False):
        return False
    proof_key = 'validated-aac:' + video_id
    success = False
    try:
        cache = get_stream_cache(server)
        if cache.backend_name != 'r2':
            return False
        ready = cache.head_variant(video_id, 'aac-v1')
        if ready and current_audio_index(ready) and cached_resource_version(ready):
            success = True
            return True
        # Prefer the original AAC copy when it exists; avoid another lossy
        # encode merely because an Opus derivative was the normal preference.
        original = cache.head_variant(video_id, 'seek-v2')
        head = (original if original and original.get('content_type', '').split(';')[0] == 'audio/mp4'
                else cache.head(video_id))
        if not head:
            return False
        length = int(head.get('content_length') or 0)
        if not 0 < length <= int(getattr(server, 'AURALIS_STREAM_CACHE_MAX_BYTES', 80*1024*1024)):
            return False
        with tempfile.TemporaryDirectory(prefix='cached-aac-') as folder:
            source, target = Path(folder)/'source.bin', Path(folder)/'indexed.m4a'
            facts = _read_cached_audio(cache, video_id, head, str(source), streams=True)
            audio = [s for s in facts.get('streams', []) if s.get('codec_type') == 'audio']
            duration = float(facts['format']['duration'])
            if len(audio) != 1 or not 0 < duration <= 24*60*60:
                return False
            codec = ['-c:a', 'copy'] if audio[0].get('codec_name') == 'aac' else ['-c:a', 'aac', '-b:a', '128k']
            result = subprocess.run(['ffmpeg', '-v', 'error', '-threads', '1', '-i', str(source),
                '-map', '0:a:0', '-vn', *codec, '-threads', '1', '-movflags', '+faststart',
                '-y', str(target)], capture_output=True, timeout=90)
            if (result.returncode or not rechunk_aac(str(target))
                    or target.stat().st_size > int(getattr(server, 'AURALIS_STREAM_CACHE_MAX_BYTES', 80*1024*1024))):
                return False
            validate_capture_file(str(target), duration, 'audio/mp4', remux=False)
            verified = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                '-of', 'json', str(target)], capture_output=True, timeout=30)
            if verified.returncode:
                return False
            actual_duration = float(json.loads(verified.stdout)['format']['duration'])
            if abs(actual_duration-duration) > .25:
                return False
            success = bool(cache.put_file(video_id, str(target), content_type='audio/mp4', metadata={
                'cache_variant':'aac-v1', 'audio_codec':'aac', 'seek_index':'v2',
                'validated':'capture-v1', 'duration':str(actual_duration),
                'parent_etag':str(head.get('etag') or ''), 'parent_variant':cached_variant(head),
            }))
            return success
    except Exception:
        return False
    finally:
        if not success:
            with server.stream_info_lock:
                keys = [key for key in server.stream_info_cache if key.startswith('validated-aac:')]
                for key in keys[:-127]:
                    server.stream_info_cache.pop(key, None)
                server.stream_info_cache[proof_key] = {'expires_at':time.time()+60, 'payload':{'valid':False}}
        _cached_repair_slot.release()


def preferred_cached_audio(server, video_id, preferred='', *, schedule=False, fallback=None):
    """A preference can select a complete file, never delay Play for conversion."""
    cache = get_stream_cache(server)
    if cache.backend_name == 'r2' and preferred == 'aac-v1' and aac_playback_enabled():
        ready = prepared_cached_object(server, video_id, 'aac-v1') or cache.head_variant(video_id, 'aac-v1')
        if ready and current_audio_index(ready) and cached_resource_version(ready):
            return ready
    head = (cache.head_variant(video_id, 'opus-v1') if preferred == 'opus-v1'
            and cache.backend_name == 'r2' else None)
    if head and not (current_audio_index(head) and cached_resource_version(head)):
        head = None
    head = head or prepared_cached_object(server, video_id) or fallback or cache.head(video_id)
    if head and schedule and preferred == 'aac-v1' and aac_playback_enabled():
        schedule_cached_audio_repair(server, video_id, head, target_variant='aac-v1')
    return head


def remember_aac_demand(server, video_id):
    if not aac_playback_enabled():
        return
    with server.stream_info_lock:
        keys = [key for key in server.stream_info_cache if key.startswith('aac-demand:')]
        for key in keys[:-63]:
            server.stream_info_cache.pop(key, None)
        server.stream_info_cache['aac-demand:' + video_id] = {
            'expires_at':time.time()+1200, 'payload':{'preferred_variant':'aac-v1'}}


def schedule_cached_audio_repair(server, video_id, head, *, target_variant=''):
    """Repair legacy objects off playback's response path using the warm owner."""
    aac = target_variant == 'aac-v1'
    if ((not aac_playback_enabled() if aac else not _needs_cached_repair(head))
            or not hasattr(server, 'stream_warm_executor')
            or not hasattr(server, 'stream_info_inflight')):
        return
    prefix = 'cache-aac:' if aac else 'cache-repair:'
    key = prefix + video_id
    with server.stream_info_lock:
        proof = server.stream_info_cache.get(('validated-aac:' + video_id)
                                             if aac else _cache_proof_key(video_id, head))
        if proof and proof['expires_at'] > time.time() and proof.get('payload', {}).get('valid') is False:
            return
        if key in server.stream_info_inflight or sum(
                k.startswith(('cache-repair:', 'cache-aac:')) for k in server.stream_info_inflight) >= 8:
            return
        pending = Future()
        server.stream_info_inflight[key] = pending
        if getattr(server, '_cached_repair_running', False):
            return
        server._cached_repair_running = True
    def retire():
        with server.stream_info_lock:
            abandoned = [server.stream_info_inflight.pop(k) for k in list(server.stream_info_inflight)
                         if k.startswith(('cache-repair:', 'cache-aac:'))]
            server._cached_repair_running = False
        for item in abandoned:
            if not item.done():
                item.set_result(False)
    def repair():
        # One warm-executor task drains a bounded FIFO. Preparing a neighbour
        # while conversion is busy must not silently discard its upgrade.
        while True:
            with server.stream_info_lock:
                selected = next((k for k in server.stream_info_inflight if k.startswith(('cache-repair:', 'cache-aac:'))), None)
                if selected is None:
                    server._cached_repair_running = False
                    return
                item = server.stream_info_inflight[selected]
            kind, source_id = selected.split(':', 1)
            started = time.perf_counter()
            try:
                valid = (validate_cached_aac_track(server, source_id) if kind == 'cache-aac'
                         else validate_cached_track(server, source_id))
            except Exception:
                valid = False
            if not item.done():
                item.set_result(valid)
            print(f'[EBB:cache-upgrade] source={source_id} variant={kind} result={"ready" if valid else "unavailable"} '
                  f'ms={round((time.perf_counter()-started)*1000)}', flush=True)
            with server.stream_info_lock:
                if server.stream_info_inflight.get(selected) is item:
                    server.stream_info_inflight.pop(selected)
    try:
        future = server.stream_warm_executor.submit(repair)
        def cancelled(task):
            if task.cancelled():
                retire()
        future.add_done_callback(cancelled)
    except Exception:
        retire()
