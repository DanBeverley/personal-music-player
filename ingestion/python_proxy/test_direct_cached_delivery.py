"""Direct bytes use only create-only derivatives, never mutable legacy audio."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
import time
import pytest
from auralis_backend.api import stream_cache as cache_module, stream_core_runtime as core
from test_cached_aac import audio


def backend():
    cache = object.__new__(cache_module.R2StreamCache)
    cache.bucket, cache.prefix = 'fixture', 'streams'
    cache._client = Mock()
    cache._client.generate_presigned_url.return_value = 'https://' + 'a' * 32 + '.r2.cloudflarestorage.com/fixture/object?private=capability'
    return cache


@pytest.mark.parametrize('variant', ['aac-v1', 'opus-v1'])
def test_only_create_only_validated_derivatives_are_signed(monkeypatch, variant):
    monkeypatch.setenv('NEATIE_DIRECT_CACHED_AUDIO', '1')
    cache = backend()
    item = audio(variant)
    with patch.object(cache_module.time, 'time', return_value=1000):
        delivery = cache.direct_delivery('generated', item)
    assert delivery['delivery_version'] == cache_module.cached_resource_version(item)
    assert delivery['delivery_expires_at'] == 4600
    args = cache._client.generate_presigned_url.call_args
    assert args.kwargs['Params']['Key'] == item['key']
    assert args.kwargs['ExpiresIn'] == 3600
    cache._client.head_object.assert_not_called()


@pytest.mark.parametrize('change', [
    {'key': 'streams/generated.bin'}, {'key': 'streams/other.bin.aac-v1'},
    {'etag': ''}, {'content_type': 'video/mp4'}, {'metadata': {}},
])
def test_unproven_or_mutable_objects_keep_validator_relay(monkeypatch, change):
    monkeypatch.setenv('NEATIE_DIRECT_CACHED_AUDIO', '1')
    cache = backend()
    assert cache.direct_delivery('generated', audio(**change)) == {}
    cache._client.generate_presigned_url.assert_not_called()


def test_opt_in_and_signing_failure_preserve_ordinary_delivery(monkeypatch, capsys):
    cache = backend()
    monkeypatch.delenv('NEATIE_DIRECT_CACHED_AUDIO', raising=False)
    assert cache.direct_delivery('generated', audio()) == {}
    monkeypatch.setenv('NEATIE_DIRECT_CACHED_AUDIO', '1')
    cache._client.generate_presigned_url.side_effect = RuntimeError('private-capability')
    assert cache.direct_delivery('generated', audio()) == {}
    assert 'private-capability' not in capsys.readouterr().out


def test_optional_direct_descriptor_preserves_preparation_for_legacy_clients(monkeypatch):
    monkeypatch.setenv('NEATIE_DIRECT_CACHED_AUDIO', '1')
    cache, server = backend(), SimpleNamespace(STREAM_CHUNK_TTL_SECONDS=1200)
    with patch.object(core, 'get_stream_cache', return_value=cache), \
         patch.object(core, 'prepared_cached_object', return_value=audio()), \
         patch.object(core, 'schedule_cached_audio_repair') as repair, \
         patch.object(core, 'warm_cached_object_prefix', return_value={
             'bytes': b'prefix', 'startup_ready': True, 'object_proven_until': time.time()+600}) as warm, \
         patch.object(core, 'record_prepare_metric'):
        result = core.prepare_stream_track(server, 'generated', 65536, True)
    assert result['delivery_url'] and not result['chunk_deferred']
    assert result['cached_prefix_bytes'] == 6 and result['startup_ready_until'] > time.time()
    assert 'cache_version=' in result['playback_path']
    warm.assert_called_once()
    repair.assert_called_once()


@pytest.mark.parametrize('variant', ['opus-v1', 'aac-v1'])
def test_all_direct_eligible_writes_are_create_only(tmp_path, variant):
    cache = backend()
    path = tmp_path / 'generated.audio'
    path.write_bytes(b'generated fixture')
    item = audio(variant)
    with patch.object(cache, 'invalidate'):
        assert cache.put_file('generated', str(path), content_type=item['content_type'], metadata=item['metadata'])
    args = cache._client.put_object.call_args.kwargs
    assert args['IfNoneMatch'] == '*'
    assert args['Key'] == item['key']
    cache._client.upload_file.assert_not_called()
