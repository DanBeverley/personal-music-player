"""Initial delivery must keep exact-file/continuation and legacy contracts."""
from threading import Lock
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import Mock
import pytest
from starlette.requests import Request
from fastapi import HTTPException
from auralis_backend.api import stream_runtime as runtime, stream_core_runtime as core
from test_direct_cached_delivery import backend
from test_cached_aac import audio

def request(**extra):
    values={'preferred_cached_variant':'aac-v1','direct_cached':'1','playback_session':'generated-session',**extra}
    byte_range=values.pop('range',None)
    return Request({'type':'http','method':'GET','path':'/playback/stream/generated',
        'query_string':urlencode(values).encode(),
        'headers':[(b'range',byte_range.encode())] if byte_range else []})

@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('NEATIE_DIRECT_CACHED_AUDIO','1')
    cache=backend();head=audio('aac-v1');head['content_length']=1024
    server=SimpleNamespace(stream_info_lock=Lock(),stream_info_cache={})
    monkeypatch.setattr(runtime,'get_stream_cache',lambda _:cache)
    monkeypatch.setattr(core,'preferred_cached_audio',lambda *a,**kw:dict(head))
    monkeypatch.setattr(core,'schedule_cached_audio_repair',lambda *a,**kw:None)
    return cache,head,server

def test_initial_range_redirect_pins_exact_cached_object_without_reading_audio(setup):
    cache,head,server=setup
    response=runtime._proxy_stream(server,'generated',request(range='bytes=0-1'))
    assert response.status_code==307 and response.headers['cache-control']=='no-store'
    assert response.headers['location'].startswith('https://')
    assert server.stream_info_cache['selected:generated-session:generated']['payload']['cached_object']==head
    cache._client.get_object.assert_not_called()
    # A continuation retains the first file even if current cache preference changes.
    response=runtime._proxy_stream(server,'generated',request(range='bytes=128-255'))
    assert response.status_code==307
    assert cache._client.generate_presigned_url.call_count==1

def test_unknown_session_continuation_cannot_redirect_to_a_new_file(setup):
    cache,_,server=setup
    with pytest.raises(HTTPException) as failure:
        runtime._proxy_stream(server,'generated',request(range='bytes=128-255'))
    assert failure.value.status_code==410
    cache._client.generate_presigned_url.assert_not_called()

@pytest.mark.parametrize('mode',['legacy-client','disabled','unverified'])
def test_ineligible_redirect_preserves_existing_relay(setup,monkeypatch,mode):
    cache,head,server=setup
    if mode=='disabled':monkeypatch.delenv('NEATIE_DIRECT_CACHED_AUDIO')
    if mode=='unverified':head['metadata']={}
    class RelayReached(HTTPException):
        def __init__(self):super().__init__(status_code=418,detail='fixture relay reached')
    monkeypatch.setattr(core,'schedule_cached_audio_repair',Mock(side_effect=RelayReached))
    with pytest.raises(RelayReached):
        runtime._proxy_stream(server,'generated',request(direct_cached='' if mode=='legacy-client' else '1'))

def test_pinned_live_representation_is_not_replaced_by_a_cached_redirect(setup):
    cache,_,server=setup
    with pytest.raises(HTTPException) as failure:
        runtime._proxy_stream(server,'generated',request(stream_version='a'*64))
    assert failure.value.status_code==410
    cache._client.generate_presigned_url.assert_not_called()
