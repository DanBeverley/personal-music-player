from __future__ import annotations

from types import SimpleNamespace

import pytest

from auralis_backend.lyrics.service import LyricsService


class _Response:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Session:
    def __init__(self, response) -> None:
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.response.pop(0) if isinstance(self.response, list) else self.response
        if isinstance(response, Exception):
            raise response
        return response


class _YTMusic:
    def __init__(self, *, watch=None, lyrics=None, error=None) -> None:
        self.watch = watch if watch is not None else {}
        self.lyrics = lyrics
        self.error = error
        self.watch_ids = []

    def get_watch_playlist(self, *, videoId, limit):
        self.watch_ids.append(videoId)
        if self.error is not None:
            raise self.error
        return self.watch

    def get_lyrics(self, browse_id, *, timestamps):
        assert timestamps is True
        return self.lyrics


class _Server:
    def __init__(self, *, ytmusic, session) -> None:
        self.ytmusic = ytmusic
        self.upstream_http = session


def _service(*, ytmusic=None, lrclib=None) -> tuple[LyricsService, _Server]:
    server = _Server(
        ytmusic=ytmusic or _YTMusic(),
        session=_Session(lrclib or _Response(404)),
    )
    return LyricsService(server), server


def test_canonical_track_uses_verified_youtube_source_not_canonical_key(monkeypatch):
    track_key = "recording:980a426e-623e-4ea5-98c7-008d037a0508"
    youtube_id = "7374CZQoS2Y"
    ytmusic = _YTMusic(
        watch={"lyrics": "MPLYt_lyrics"},
        lyrics={
            "lyrics": "First line\nSecond line",
            "source": "Source: LyricFind",
            "hasTimestamps": False,
        },
    )
    service, _ = _service(ytmusic=ytmusic)

    monkeypatch.setattr(
        "auralis_backend.api.stream_runtime.playback_source_candidates",
        lambda _server, requested: [
            {
                "provider": "youtube",
                "source_id": youtube_id,
                "verification_state": "verified",
            }
        ]
        if requested == track_key
        else [],
    )

    result = service.get(track_key, title="No Surprises", artist="Radiohead")

    assert result["lyrics_status"] == "found"
    assert result["resolved_source_id"] == youtube_id
    assert ytmusic.watch_ids == [youtube_id]
    assert track_key not in ytmusic.watch_ids


def test_ytmusic_failure_falls_through_to_lrclib_synced_lyrics():
    service, server = _service(
        ytmusic=_YTMusic(error=TimeoutError("provider timeout")),
        lrclib=_Response(
            200,
            {
                "instrumental": False,
                "syncedLyrics": "[00:01.00]First line\n[00:03.50]Second line",
                "plainLyrics": "First line\nSecond line",
            },
        ),
    )

    result = service.get(
        "7374CZQoS2Y",
        title="No Surprises",
        artist="Radiohead",
        album="OK Computer",
        duration=229,
    )

    assert result["lyrics_status"] == "found"
    assert result["source"] == "LRCLIB"
    assert result["has_timestamps"] is True
    assert result["lines"][0]["start_time_ms"] == 1000
    assert result["lines"][0]["end_time_ms"] == 3500
    request_params = server.upstream_http.calls[0][1]["params"]
    assert request_params == {
        "track_name": "No Surprises",
        "artist_name": "Radiohead",
        "album_name": "OK Computer",
        "duration": 229,
    }


def test_lrclib_instrumental_is_not_reported_as_missing_lyrics():
    service, _ = _service(
        lrclib=_Response(
            200,
            {
                "instrumental": True,
                "syncedLyrics": None,
                "plainLyrics": None,
            },
        )
    )

    result = service.get(
        "recording:instrumental",
        title="Instrumental",
        artist="Composer",
    )

    assert result["lyrics_status"] == "instrumental"
    assert result["has_lyrics"] is False


def test_lrclib_structured_search_recovers_a_safe_exact_recording_match():
    service, server = _service(
        lrclib=[
            _Response(404),
            _Response(
                200,
                [
                    {
                        "trackName": "No Surprises",
                        "artistName": "Cover Artist",
                        "albumName": "Covers",
                        "duration": 229,
                        "plainLyrics": "Wrong recording",
                    },
                    {
                        "trackName": "No Surprises",
                        "artistName": "Radiohead",
                        "albumName": "OK Computer",
                        "duration": 229,
                        "plainLyrics": "Correct recording",
                    },
                ],
            ),
        ]
    )

    result = service.get(
        "recording:980a426e-623e-4ea5-98c7-008d037a0508",
        title="No Surprises",
        artist="Radiohead",
        album="OK Computer",
        duration=229,
    )

    assert result["lyrics_status"] == "found"
    assert result["lines"][0]["text"] == "Correct recording"
    assert [call[0].rsplit("/", 1)[-1] for call in server.upstream_http.calls] == [
        "get",
        "search",
    ]


def test_unresolvable_identity_is_distinct_from_provider_failure(monkeypatch):
    service, _ = _service()
    monkeypatch.setattr(
        "auralis_backend.api.stream_runtime.playback_source_candidates",
        lambda _server, _requested: [],
    )

    result = service.get("recording:missing")

    assert result["lyrics_status"] == "identity_unresolved"
    assert result["provider_attempts"] == []


@pytest.mark.parametrize(
    ("session_result", "expected"),
    [
        (_Response(404), "not_found"),
        (_Response(503), "provider_error"),
        (TimeoutError("offline"), "provider_error"),
    ],
)
def test_lrclib_miss_and_failure_states_are_explicit(session_result, expected):
    service, _ = _service(lrclib=session_result)

    result = service.get(
        "recording:no-provider-result",
        title="Unknown song",
        artist="Unknown artist",
    )

    assert result["lyrics_status"] == expected
    assert result["has_lyrics"] is False


def test_successful_result_is_cached_without_repeating_provider_calls():
    ytmusic = _YTMusic(
        watch={"lyrics": "MPLYt_lyrics"},
        lyrics={
            "lyrics": [
                SimpleNamespace(text="First line", start_time=1000, end_time=2000)
            ],
            "source": "Source: LyricFind",
            "hasTimestamps": True,
        },
    )
    service, _ = _service(ytmusic=ytmusic)

    first = service.get("7374CZQoS2Y")
    second = service.get("7374CZQoS2Y")

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert ytmusic.watch_ids == ["7374CZQoS2Y"]


def test_force_refresh_bypasses_a_transient_cached_provider_failure():
    ytmusic = _YTMusic(error=TimeoutError("temporary"))
    service, _ = _service(ytmusic=ytmusic, lrclib=_Response(503))

    failed = service.get(
        "7374CZQoS2Y",
        title="No Surprises",
        artist="Radiohead",
    )
    ytmusic.error = None
    ytmusic.watch = {"lyrics": "MPLYt_lyrics"}
    ytmusic.lyrics = {
        "lyrics": "Recovered line",
        "source": "Source: LyricFind",
        "hasTimestamps": False,
    }
    recovered = service.get(
        "7374CZQoS2Y",
        title="No Surprises",
        artist="Radiohead",
        force_refresh=True,
    )

    assert failed["lyrics_status"] == "provider_error"
    assert recovered["lyrics_status"] == "found"
    assert ytmusic.watch_ids == ["7374CZQoS2Y", "7374CZQoS2Y"]
