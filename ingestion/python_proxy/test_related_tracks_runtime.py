from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from auralis_backend.api.media_runtime import get_related_tracks
from auralis_backend.api.media_runtime import get_album_details
from auralis_backend.api.stream_runtime import playback_resolve


def test_watch_parser_keeps_tracks_when_optional_related_endpoint_is_missing():
    from ytmusicapi import YTMusic
    from auralis_backend.search.upstream_runtime import CompatibleYTMusic
    import copy

    track = {"playlistPanelVideoRenderer": {
        "videoId": "video000001", "title": {"runs": [{"text": "Related track"}]},
        "thumbnail": {"thumbnails": []}, "menu": {"menuRenderer": {"items": []}},
        "longBylineText": {"runs": [{"text": "Artist", "navigationEndpoint": {
            "browseEndpoint": {"browseId": "UC_artist"}}}]},
    }}
    tabs = [{"tabRenderer": {"content": {"musicQueueRenderer": {"content": {
        "playlistPanelRenderer": {"contents": [track]}}}}}},
        {"tabRenderer": {"unselectable": True}}, {"tabRenderer": {"title": "Related"}}]
    client = object.__new__(CompatibleYTMusic)
    for optional_id in (None, "related-browse-id"):
        current = copy.deepcopy(tabs)
        if optional_id:
            current[2]["tabRenderer"]["endpoint"] = {"browseEndpoint": {"browseId": optional_id}}
        response = {"contents": {"singleColumnMusicWatchNextResultsRenderer": {
            "tabbedRenderer": {"watchNextTabbedResultsRenderer": {"tabs": current}}}}}
        with patch.object(YTMusic, "_send_request", return_value=response):
            result = client.get_watch_playlist(videoId="seed0000001", radio=True, limit=50)
        assert [item["videoId"] for item in result["tracks"]] == ["video000001"]
        assert result["related"] == optional_id


class _YtMusic:
    def get_watch_playlist(self, *, videoId: str, radio: bool, limit: int):
        assert videoId == "B_HSa1dEL9s"
        assert radio is True
        assert limit >= 50
        return {
            "tracks": [
                {
                    "videoId": f"video00000{index}",
                    "title": f"Related {index}",
                    "artists": [{"name": "Metallica"}],
                    "duration_seconds": 240,
                    "album": {"name": "Related Album", "id": "album-1"},
                }
                for index in range(1, 5)
            ]
        }


class _Server:
    ytmusic = _YtMusic()

    @staticmethod
    def extract_artist(entry):
        return entry["artists"][0]["name"]

    @staticmethod
    def extract_album_info(entry):
        album = entry.get("album") or {}
        return {"title": album.get("name"), "id": album.get("id")}

    @staticmethod
    def extract_thumbnail(_entry):
        return "https://img.example/cover.jpg"

    @staticmethod
    def parse_duration_seconds(value):
        return int(value or 0)


def test_related_tracks_resolves_canonical_seed_and_pages_results() -> None:
    request = SimpleNamespace(
        track_key="recording:771ab043-8821-44f9-b8e0-2733c3126c6d",
        avoid_ids=[],
        offset=1,
        limit=2,
    )
    with patch(
        "auralis_backend.api.stream_runtime.playback_source_candidates",
        return_value=[{"provider": "youtube", "source_id": "B_HSa1dEL9s"}],
    ):
        payload = get_related_tracks(_Server(), request)

    assert payload["status"] == "ready"
    assert [track["title"] for track in payload["tracks"]] == [
        "Related 2",
        "Related 3",
    ]
    assert payload["has_more"] is True
    assert payload["next_cursor"] == "3"


def test_related_tracks_fetches_beyond_previously_seen_provider_page() -> None:
    class Provider:
        def get_watch_playlist(self, *, videoId, radio, limit):
            assert radio
            assert limit > 60
            return {"tracks": [
                {"videoId": f"video{i:06}", "title": f"Track {i}",
                 "artists": [{"name": "Artist"}]} for i in range(limit)
            ]}
    server = _Server()
    server.ytmusic = Provider()
    request = SimpleNamespace(track_key="B_HSa1dEL9s", offset=0, limit=18,
        avoid_ids=[f"video{i:06}" for i in range(60)])
    with patch("auralis_backend.api.stream_runtime.playback_source_candidates", return_value=[]):
        result = get_related_tracks(server, request)
    assert len(result["tracks"]) == 18
    assert not set(request.avoid_ids) & {track["id"] for track in result["tracks"]}


def test_related_tracks_reports_transient_source_shortage() -> None:
    request = SimpleNamespace(
        track_key="recording:missing",
        avoid_ids=[],
        offset=0,
        limit=18,
    )
    with patch(
        "auralis_backend.api.stream_runtime.playback_source_candidates",
        return_value=[],
    ):
        payload = get_related_tracks(_Server(), request)

    assert payload["status"] == "temporarily_unavailable"
    assert payload["has_more"] is True
    assert payload["tracks"] == []


def test_related_tracks_uses_physical_hint_to_repair_canonical_source() -> None:
    request = SimpleNamespace(
        track_key="recording:771ab043-8821-44f9-b8e0-2733c3126c6d",
        title="For Whom the Bell Tolls",
        artist="Metallica",
        album="Ride the Lightning",
        duration=310,
        source_id="B_HSa1dEL9s",
        source_provider="youtube",
        avoid_ids=[],
        offset=0,
        limit=2,
    )
    with patch(
        "auralis_backend.api.stream_runtime.playback_source_candidates",
        return_value=[],
    ), patch(
        "auralis_backend.discovery.source_registry.resolve_user_requested_playback_source",
        return_value={
            "provider": "youtube",
            "source_id": "B_HSa1dEL9s",
            "verification_state": "verified",
        },
    ) as resolve:
        payload = get_related_tracks(_Server(), request)

    assert resolve.call_count == 1
    assert resolve.call_args.kwargs == {
        "track_key": request.track_key,
        "title": request.title,
        "artist": request.artist,
        "album": request.album,
        "duration": request.duration,
        "source_id": request.source_id,
        "source_provider": request.source_provider,
    }
    assert payload["status"] == "ready"
    assert payload["source_id"] == "B_HSa1dEL9s"
    assert len(payload["tracks"]) == 2


def test_playback_resolve_repairs_missing_canonical_source_before_streaming() -> None:
    server = _Server()
    with patch(
        "auralis_backend.api.stream_runtime.playback_source_candidates",
        return_value=[],
    ), patch(
        "auralis_backend.discovery.source_registry.resolve_user_requested_playback_source",
        return_value={
            "provider": "youtube",
            "source_id": "B_HSa1dEL9s",
            "authority": "topic",
            "verification_state": "verified",
        },
    ) as resolve, patch(
        "auralis_backend.api.stream_runtime.get_stream_info",
        return_value={
            "url": "https://stream.example/audio",
            "mime_type": "audio/mp4",
            "duration": 310,
            "expires_at": 9999999999.0,
        },
    ):
        payload = playback_resolve(
            server,
            "recording:771ab043-8821-44f9-b8e0-2733c3126c6d",
            title="For Whom the Bell Tolls",
            artist="Metallica",
            album="Ride the Lightning",
            duration=310,
            source_id="B_HSa1dEL9s",
        )

    assert resolve.call_count == 1
    assert payload["source_id"] == "B_HSa1dEL9s"
    assert payload["url"].startswith("/playback/stream/")


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def execute(self, _query, params):
        assert "MPREb_album" in params
        return _Rows([
            [
                '{"title":"Hotel California","artist":"Eagles",'
                '"tracks":[{"track_key":"recording:one","title":"Hotel California",'
                '"artist":"Eagles","playable":true}]}'
            ]
        ])

    def close(self):
        pass


def test_album_details_uses_persisted_tracks_for_provider_prefixed_id() -> None:
    with patch(
        "auralis_backend.recommend.store_runtime.open_recommendation_store_connection",
        return_value=_Connection(),
    ):
        payload = get_album_details(_Server(), "ytmusic:album:MPREb_album")

    assert payload["status"] == "success"
    assert payload["track_count"] == 1
    assert payload["tracks"][0]["track_key"] == "recording:one"
