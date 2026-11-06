
from auralis_backend.api import stream_sources, stream_buffers, stream_cached_audio
"""AAC variant contracts using synthetic metadata; no real media or provider data."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
from threading import Lock
from pathlib import Path
import json
import time
import hashlib

import pytest
from botocore.exceptions import ClientError

from auralis_backend.api import stream_cache as cache_module
from auralis_backend.api import stream_core_runtime as core
from auralis_backend.api import stream_runtime as runtime


def audio(variant='aac-v1', **changes):
    tags = {'cache_variant': variant, 'validated': 'capture-v1',
            'seek_index': 'v2', 'audio_codec': 'aac' if variant == 'aac-v1' else 'opus',
            'duration': '123.5'}
    result = {'backend': 'r2', 'key': 'streams/generated.bin.' + variant,
              'etag': 'etag-one', 'content_length': 8,
              'content_type': 'audio/mp4' if variant == 'aac-v1' else 'audio/webm',
              'metadata': tags}
    result.update(changes)
    return result


def test_aac_is_opt_in_and_default_selection_does_not_probe_aac(monkeypatch):
    monkeypatch.delenv('NEATIE_AAC_PLAYBACK', raising=False)
    assert not cache_module.aac_playback_enabled()
    fallback = audio('opus-v1')
    backend = Mock(backend_name='r2')
    backend.head.return_value = fallback
    with patch.object(stream_cached_audio, 'get_stream_cache', return_value=backend), \
         patch.object(stream_cached_audio, 'prepared_cached_object', return_value=None), \
         patch.object(stream_cached_audio, 'schedule_cached_audio_repair') as schedule:
        assert core.preferred_cached_audio(object(), 'generated', 'aac-v1', schedule=True) == fallback
    backend.head_variant.assert_not_called()
    schedule.assert_not_called()


def test_aac_preference_uses_ready_pinned_file_or_returns_opus_while_scheduling(monkeypatch):
    monkeypatch.setenv('NEATIE_AAC_PLAYBACK', '1')
    fallback, ready = audio('opus-v1'), audio()
    backend = Mock(backend_name='r2')
    backend.head.return_value = fallback
    backend.head_variant.return_value = None
    with patch.object(stream_cached_audio, 'get_stream_cache', return_value=backend), \
         patch.object(stream_cached_audio, 'prepared_cached_object', return_value=None), \
         patch.object(stream_cached_audio, 'schedule_cached_audio_repair') as schedule:
        assert core.preferred_cached_audio(object(), 'generated', 'aac-v1', schedule=True) == fallback
        assert schedule.call_args.kwargs['target_variant'] == 'aac-v1'
        backend.head_variant.return_value = ready
        assert core.preferred_cached_audio(object(), 'generated', 'aac-v1', schedule=True) == ready
    descriptor = core.cached_stream_descriptor(backend, 'generated', ready)
    assert 'cache_variant=aac-v1' in descriptor['playback_path']
    assert 'cache_version=' + cache_module.cached_resource_version(ready) in descriptor['playback_path']
    assert descriptor['duration'] == 123.5
    assert descriptor['mime_type'] == 'audio/mp4'


@pytest.mark.parametrize('field,value', [
    ('cache_variant', 'opus-v1'), ('cache_variant', None), ('audio_codec', 'opus'),
    ('validated', 'other'), ('seek_index', 'v1'), ('duration', 'NaN'),
    ('duration', '0'), ('duration', '-1'), ('duration', '86401'),
])
def test_aac_manifest_rejects_contradictory_or_missing_proof(field, value):
    metadata = audio()
    if value is None:
        metadata['metadata'].pop(field)
    else:
        metadata['metadata'][field] = value
    assert not cache_module.current_audio_index(metadata)


def test_aac_requires_mp4_and_a_versionable_object(monkeypatch):
    monkeypatch.setenv('NEATIE_AAC_PLAYBACK', '1')
    assert not cache_module.current_audio_index(audio(content_type='audio/webm'))
    backend = Mock(backend_name='r2')
    backend.head_variant.return_value = audio(etag='')
    fallback = audio('opus-v1')
    backend.head.return_value = fallback
    with patch.object(stream_cached_audio, 'get_stream_cache', return_value=backend), \
         patch.object(stream_cached_audio, 'prepared_cached_object', return_value=None), \
         patch.object(stream_cached_audio, 'schedule_cached_audio_repair'):
        assert core.preferred_cached_audio(object(), 'generated', 'aac-v1', schedule=True) == fallback


def test_cold_preparation_does_not_wait_for_aac_lookup():
    sources = {'track': [{'provider':'youtube', 'source_id':'generated'}]}
    resolved = {'generated': {'prepared':True, 'cache_tier':'cold',
                              'playback_path':'/playback/stream/generated'}}
    server = SimpleNamespace(stream_info_lock=Lock(), stream_info_cache={})
    with patch.object(runtime, 'playback_source_candidates_batch', return_value=sources), \
         patch.object(runtime, 'prepare_streams_with_failures', return_value=(resolved, {})), \
         patch.object(core, 'preferred_cached_audio') as preferred:
        result, failed = runtime.prepare_playback_tracks(server, ['track'], limit=1,
            preferred_cached_variant='aac-v1')
    assert not failed and result['track']['playback_path'] == '/playback/stream/generated'
    preferred.assert_not_called()


def test_active_aac_preparation_warms_selected_bytes_once_and_reuses_proof(monkeypatch):
    monkeypatch.setenv('NEATIE_AAC_PLAYBACK', '1')
    backend = Mock(backend_name='r2')
    backend.head.return_value = audio('opus-v1')
    backend.head_variant.return_value = audio()
    backend.direct_delivery.return_value = {}
    backend.iter_object.return_value = [b'aacbytes']
    server = SimpleNamespace(PREPARE_SESSION_WORKERS=2, STREAM_CHUNK_TTL_SECONDS=1200,
        stream_info_lock=Lock(), stream_info_cache={}, prepare_metrics_lock=Lock(), prepare_metrics=[],
        stream_chunk_lock=Lock(), stream_chunk_cache={}, stream_chunk_inflight_lock=Lock(),
        stream_chunk_inflight={})
    sources = {'track': [{'provider':'youtube', 'source_id':'generated'}]}
    with patch.object(runtime, 'playback_source_candidates_batch', return_value=sources), \
         patch.object(core, 'get_stream_cache', return_value=backend), \
         patch.object(stream_cached_audio, 'get_stream_cache', return_value=backend), \
         patch.object(core, 'schedule_cached_audio_repair'), \
         patch.object(stream_cached_audio, 'schedule_cached_audio_repair'):
        for _ in range(2):
            prepared, failed = runtime.prepare_playback_tracks(server, ['track'], limit=1,
                active_queue=True, preferred_cached_variant='aac-v1')
            assert not failed
            value = prepared['track']
            assert 'cache_variant=aac-v1' in value['playback_path']
            assert value['cached_prefix_bytes'] == 8
            assert value['chunk_cache_hit'] and not value['chunk_deferred']
            assert core.prepared_cached_object(server, 'generated', 'aac-v1') == audio()
        backend.iter_object.assert_called_once_with('generated', start=0, end=7, expected=audio())
        assert all(m['cached_prefix_bytes'] == 8 for m in server.prepare_metrics)
        # Safari's initial probe must reuse that proof, not repeat HEAD/range I/O.
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from starlette.requests import Request
        app = FastAPI()
        @app.get('/playback/stream/{key}')
        def stream(key: str, request: Request):
            return runtime._proxy_stream(server, key, request)
        backend.reset_mock()
        with patch.object(runtime, 'get_stream_cache', return_value=backend), TestClient(app) as client:
            response = client.get(value['playback_path'], headers={'Range':'bytes=0-1'})
        assert response.status_code == 206 and response.content == b'aa'
        backend.head.assert_not_called()
        backend.head_variant.assert_not_called()
        backend.iter_object.assert_not_called()


def test_cold_source_does_not_probe_optional_cached_variant():
    backend = Mock(backend_name='r2')
    backend.head.return_value = None
    server = SimpleNamespace(stream_chunk_lock=Lock(), stream_chunk_cache={})
    with patch.object(core, 'get_stream_cache', return_value=backend), \
         patch.object(core, 'is_stream_info_cached', return_value=False), \
         patch.object(core, 'get_stream_info', side_effect=LookupError('cold control')), \
         patch.object(core, 'preferred_cached_audio') as preferred:
        with pytest.raises(LookupError):
            core.prepare_stream_track(server, 'cold', 65536, True,
                preferred_cached_variant='aac-v1')
        preferred.assert_not_called()
        backend.head_variant.assert_not_called()


def test_valid_aac_does_not_schedule_conversion_back_to_opus(monkeypatch):
    monkeypatch.setenv('NEATIE_OPUS_PLAYBACK', '1')
    assert not stream_cached_audio._needs_cached_repair(audio())
    assert not stream_cached_audio._needs_cached_repair(audio('opus-v1'))
    invalid = audio(); invalid['metadata']['seek_index'] = 'old'
    assert stream_cached_audio._needs_cached_repair(invalid)


def test_aac_demand_is_flagged_expiring_and_bounded(monkeypatch):
    server = SimpleNamespace(stream_info_lock=Lock(), stream_info_cache={'unrelated': {'payload':'kept'}})
    monkeypatch.delenv('NEATIE_AAC_PLAYBACK', raising=False)
    core.remember_aac_demand(server, 'off')
    assert 'aac-demand:off' not in server.stream_info_cache
    monkeypatch.setenv('NEATIE_AAC_PLAYBACK', '1')
    with patch.object(core.time, 'time', return_value=1000):
        for index in range(80):
            core.remember_aac_demand(server, str(index))
    demands = {k:v for k,v in server.stream_info_cache.items() if k.startswith('aac-demand:')}
    assert len(demands) <= 64
    assert 'aac-demand:0' not in demands and 'aac-demand:79' in demands
    assert demands['aac-demand:79']['expires_at'] == 2200
    assert server.stream_info_cache['unrelated'] == {'payload':'kept'}


def test_cold_demand_exists_before_preparation_can_publish(monkeypatch):
    monkeypatch.setenv('NEATIE_AAC_PLAYBACK', '1')
    server = SimpleNamespace(stream_info_lock=Lock(), stream_info_cache={})
    sources = {'track': [{'provider':'youtube', 'source_id':'generated'}]}
    def prepare(*_args, **_kwargs):
        assert 'aac-demand:generated' in server.stream_info_cache
        return {'generated': {'prepared':True, 'cache_tier':'cold',
                              'playback_path':'/playback/stream/generated'}}, {}
    with patch.object(runtime, 'playback_source_candidates_batch', return_value=sources), \
         patch.object(runtime, 'prepare_streams_with_failures', side_effect=prepare):
        result, failed = runtime.prepare_playback_tracks(server, ['track'], limit=1,
            preferred_cached_variant='aac-v1')
    assert not failed and result['track']['prepared']


@pytest.mark.parametrize('demand,stored,enabled,expect_schedule', [
    ('active', True, True, True), ('expired', True, True, False),
    ('missing', True, True, False), ('active', False, True, False),
    ('active', True, False, False),
])
def test_full_cache_publication_schedules_only_admitted_live_aac_demand(
        tmp_path, monkeypatch, demand, stored, enabled, expect_schedule):
    monkeypatch.setenv('NEATIE_AAC_PLAYBACK', '1' if enabled else '0')
    monkeypatch.setenv('NEATIE_OPUS_PLAYBACK', '0')
    server = SimpleNamespace(stream_info_lock=Lock(), stream_info_cache={})
    if demand != 'missing':
        server.stream_info_cache['aac-demand:generated'] = {
            'expires_at': time.time() + (1200 if demand == 'active' else -1)}
    source = tmp_path/'source'; source.write_bytes(b'stream')
    normalized = tmp_path/'normalized.webm'; normalized.write_bytes(b'indexed')
    backend = Mock(backend_name='r2')
    head = audio('opus-v1')
    backend.head.side_effect = [None, head]
    backend.put_file.return_value = stored
    probe = SimpleNamespace(returncode=0, stdout=b'{"format":{"duration":"123.5"},"streams":[{"codec_type":"audio","codec_name":"opus"}]}')
    with patch.object(cache_module, 'normalize_source_file', return_value=str(normalized)), \
         patch.object(cache_module.subprocess, 'run', return_value=probe), \
         patch.object(core, 'schedule_cached_audio_repair') as schedule:
        result = cache_module._publish_stream_file(server, backend, 'generated', str(source), 'audio/webm', None, 1000)
    assert result is stored
    if expect_schedule:
        schedule.assert_called_once_with(server, 'generated', head, target_variant='aac-v1')
    else:
        schedule.assert_not_called()


def test_reused_complete_cache_still_schedules_live_aac_demand(tmp_path, monkeypatch):
    monkeypatch.setenv('NEATIE_AAC_PLAYBACK', '1')
    monkeypatch.setenv('NEATIE_OPUS_PLAYBACK', '0')
    server = SimpleNamespace(stream_info_lock=Lock(), stream_info_cache={
        'aac-demand:generated': {'expires_at': time.time()+1200}})
    source = tmp_path/'source'; source.write_bytes(b'stream')
    existing = audio('opus-v1')
    existing['metadata']['source_sha256'] = hashlib.sha256(b'stream').hexdigest()
    backend = Mock(backend_name='r2')
    backend.head.return_value = existing
    with patch.object(core, 'schedule_cached_audio_repair') as schedule:
        assert cache_module._publish_stream_file(server, backend, 'generated', str(source), 'audio/webm', None, 1000)
    backend.put_file.assert_not_called()
    schedule.assert_called_once_with(server, 'generated', existing, target_variant='aac-v1')


def test_regular_conversion_failure_records_retry_backoff(monkeypatch):
    monkeypatch.setenv('NEATIE_AAC_PLAYBACK', '1')
    fallback = audio('opus-v1')
    backend = Mock(backend_name='r2')
    backend.head_variant.side_effect = lambda _id, _variant: None
    backend.head.return_value = fallback
    backend.iter_object.return_value = iter([b'12345678'])
    server = SimpleNamespace(stream_info_lock=Lock(), stream_info_cache={})
    failed_probe = SimpleNamespace(returncode=1, stdout=b'')
    with patch.object(stream_cached_audio, 'get_stream_cache', return_value=backend), \
         patch('subprocess.run', return_value=failed_probe):
        assert core.validate_cached_aac_track(server, 'generated') is False
    proof = server.stream_info_cache['validated-aac:generated']
    assert proof['payload']['valid'] is False
    server.stream_info_inflight = {}
    server.stream_warm_executor = Mock()
    core.schedule_cached_audio_repair(server, 'generated', fallback, target_variant='aac-v1')
    server.stream_warm_executor.submit.assert_not_called()
    assert proof['expires_at'] > time.time()


def test_immutable_collision_with_invalid_aac_is_not_reported_as_success(tmp_path):
    backend = object.__new__(cache_module.R2StreamCache)
    backend.bucket, backend.prefix = 'test', 'streams'
    backend._client = Mock()
    backend._client.put_object.side_effect = ClientError(
        {'ResponseMetadata':{'HTTPStatusCode':412}, 'Error':{'Code':'PreconditionFailed'}}, 'PutObject')
    backend.invalidate = Mock()
    invalid = audio()
    invalid['metadata']['audio_codec'] = 'opus'
    backend.head_variant = Mock(return_value=invalid)
    path = tmp_path/'audio.m4a'
    path.write_bytes(b'converted')
    assert not backend.put_file('generated', str(path), content_type='audio/mp4',
        metadata={'cache_variant':'aac-v1', 'audio_codec':'aac', 'seek_index':'v2',
                  'validated':'capture-v1', 'duration':'123.5'})


def test_pinned_aac_route_rejects_changed_version_without_upstream_fallback():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.requests import Request
    from test_stream_prepare_latency import _fake_server
    server = _fake_server()
    backend = Mock(backend_name='r2', enabled=True)
    backend.head_variant.return_value = audio(etag='new-etag')
    app = FastAPI()
    @app.get('/playback/stream/{key}')
    def stream(key: str, request: Request):
        return runtime.playback_stream(server, key, request)
    old_version = cache_module.cached_resource_version(audio())
    try:
        with patch.object(runtime, 'get_stream_cache', return_value=backend), \
             patch.object(runtime, 'prepared_cached_object', return_value=None), \
             patch.object(runtime, 'playback_source_candidates', return_value=[
                 {'provider':'youtube', 'source_id':'generated'}]), \
             patch.object(runtime, 'get_stream_info') as extract, TestClient(app) as client:
            response = client.get('/playback/stream/generated', params={
                'cache_variant':'aac-v1', 'cache_version':old_version})
            assert response.status_code == 410
            assert response.json()['detail']['code'] == 'playback_source_expired'
            extract.assert_not_called()
    finally:
        server.stream_warm_executor.shutdown(wait=True)


@pytest.mark.parametrize('codec, expected', [('aac', 'copy'), ('opus', 'aac')])
def test_conversion_probes_codec_validates_duration_and_publishes_pinned_manifest(monkeypatch, codec, expected):
    monkeypatch.setenv('NEATIE_AAC_PLAYBACK', '1')
    source = audio('opus-v1')
    backend = Mock(backend_name='r2')
    backend.head_variant.return_value = None
    backend.head.return_value = source
    backend.iter_object.return_value = iter([b'12345678'])
    backend.put_file.return_value = True
    server = SimpleNamespace(stream_info_lock=Lock(), stream_info_cache={})
    commands = []
    def run(command, **_kwargs):
        commands.append(command)
        if command[0] == 'ffprobe':
            if len(commands) > 1:
                return SimpleNamespace(returncode=0, stdout=b'{"format":{"duration":"123.5"}}')
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                'format': {'duration': '123.5'},
                'streams': [{'codec_type':'audio', 'codec_name':codec}]}).encode())
        Path(command[-1]).write_bytes(b'converted')
        return SimpleNamespace(returncode=0)
    with patch.object(stream_cached_audio, 'get_stream_cache', return_value=backend), \
         patch('subprocess.run', side_effect=run), \
         patch('auralis_backend.api.mp4_audio.rechunk_aac', return_value=True), \
         patch.object(cache_module, 'validate_capture_file') as validate:
        assert core.validate_cached_aac_track(server, 'generated')
    command = commands[1]
    assert command[command.index('-c:a')+1] == expected
    assert command[command.index('-threads')+1] == '1'
    assert command[command.index('-movflags')+1] == '+faststart'
    assert validate.call_args.args[1:] == (123.5, 'audio/mp4')
    assert validate.call_args.kwargs['remux'] is False
    manifest = backend.put_file.call_args.kwargs
    assert manifest['content_type'] == 'audio/mp4'
    assert manifest['metadata'] == {'cache_variant':'aac-v1', 'audio_codec':'aac',
        'seek_index':'v2', 'validated':'capture-v1', 'duration':'123.5',
        'parent_etag':'etag-one', 'parent_variant':'opus-v1'}


def test_cold_aac_demand_is_bounded_and_full_upload_uses_existing_warm_owner(monkeypatch):
    monkeypatch.setenv('NEATIE_AAC_PLAYBACK', '1')
    server = SimpleNamespace(stream_info_lock=Lock(), stream_info_cache={})
    for index in range(80):
        core.remember_aac_demand(server, str(index))
    assert len(server.stream_info_cache) == 64
    backend = Mock(backend_name='r2')
    backend.head.return_value = audio('opus-v1')
    with patch.object(core, 'schedule_cached_audio_repair') as schedule:
        cache_module._schedule_demanded_aac(server, backend, '79')
        schedule.assert_called_once()
        assert schedule.call_args.args == (server, '79', backend.head.return_value)
        assert schedule.call_args.kwargs == {'target_variant':'aac-v1'}
        schedule.reset_mock()
        cache_module._schedule_demanded_aac(server, backend, '0')
        schedule.assert_not_called()
