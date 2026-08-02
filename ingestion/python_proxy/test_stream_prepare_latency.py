from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from types import SimpleNamespace
import pathlib
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from fastapi import HTTPException

from auralis_backend.api import (
    media_runtime,
    stream_cache,
    stream_core_runtime,
    stream_runtime,
)


def _fake_server() -> SimpleNamespace:
    return SimpleNamespace(
        STREAM_CHUNK_TTL_SECONDS=1800,
        STREAM_INFO_TTL_SECONDS=21600,
        STREAM_WARM_CHUNK_BYTES=786432,
        PREPARE_SESSION_WORKERS=2,
        PREPARE_BACKGROUND_MAX_LOOKAHEAD=3,
        stream_info_cache={},
        stream_info_inflight={},
        stream_info_lock=Lock(),
        stream_failure_cache={},
        stream_failure_lock=Lock(),
        STREAM_FAILURE_COOLDOWN_SECONDS=900,
        AURALIS_YTDLP_COOKIES_PATH="",
        AURALIS_YTDLP_PO_TOKEN="",
        stream_chunk_cache={},
        stream_chunk_lock=Lock(),
        stream_chunk_inflight={},
        stream_chunk_inflight_lock=Lock(),
        prepare_metrics=deque(maxlen=20),
        prepare_metrics_lock=Lock(),
        stream_warm_executor=ThreadPoolExecutor(max_workers=1),
    )


class StreamPrepareLatencyTests(unittest.TestCase):
    def test_format_selection_falls_back_to_a_real_audio_format(self) -> None:
        server = _fake_server()
        server.AURALIS_YTDLP_COOKIES_PATH = "C:/test/cookies.txt"
        first = MagicMock()
        first.__enter__.return_value.extract_info.side_effect = RuntimeError(
            "Requested format is not available"
        )
        second = MagicMock()
        second.__enter__.return_value.extract_info.return_value = {
            "duration": 123,
            "http_headers": {"User-Agent": "test"},
            "formats": [
                {
                    "url": "https://example.test/video",
                    "acodec": "aac",
                    "vcodec": "h264",
                    "ext": "mp4",
                    "abr": 96,
                },
                {
                    "url": "https://example.test/audio",
                    "acodec": "mp4a",
                    "vcodec": "none",
                    "ext": "m4a",
                    "abr": 128,
                },
            ],
        }

        with patch.object(
            stream_core_runtime.yt_dlp,
            "YoutubeDL",
            side_effect=[first, second],
        ) as youtube_dl:
            result = stream_core_runtime.extract_stream_info(
                server,
                "00000000001",
            )

        self.assertEqual(result.get("url"), "https://example.test/audio")
        self.assertEqual(result.get("duration"), 123)
        first_options = youtube_dl.call_args_list[0].args[0]
        fallback_options = youtube_dl.call_args_list[1].args[0]
        self.assertEqual(
            first_options.get("format"),
            "bestaudio[acodec!=none]/best[acodec!=none]/best",
        )
        self.assertEqual(first_options.get("cookiefile"), "C:/test/cookies.txt")
        self.assertNotIn("format", fallback_options)

    def test_background_prepare_limits_speculative_resolution_to_three_tracks(self) -> None:
        server = _fake_server()
        server.PREPARE_SESSION_MAX_LOOKAHEAD = 18
        request = SimpleNamespace(
            track_keys=[f"recording:{index}" for index in range(12)],
            current_track_key="recording:0",
            active_queue=True,
            lookahead=12,
            background=True,
        )
        try:
            with patch(
                "auralis_backend.discovery.source_registry.youtube_background_resolution_blocked",
                return_value=False,
            ), patch(
                "auralis_backend.api.stream_runtime.prepare_playback_tracks",
                return_value=({}, {}),
            ) as prepare:
                media_runtime.prepare_session(server, request)
        finally:
            server.stream_warm_executor.shutdown(wait=True)

        self.assertEqual(prepare.call_args.kwargs["limit"], 3)

    def test_starting_prefixes_are_small_and_intent_weighted(self) -> None:
        server = _fake_server()

        self.assertEqual(stream_core_runtime.chunk_target_bytes(server, 0, True), 262144)
        self.assertEqual(stream_core_runtime.chunk_target_bytes(server, 1, True), 196608)
        self.assertEqual(stream_core_runtime.chunk_target_bytes(server, 2, True), 131072)

    def test_background_full_prefetch_does_not_enter_youtube_resolver_when_blocked(self) -> None:
        request = SimpleNamespace(query_params={"background": "1"}, headers={})
        with patch.object(
            stream_runtime,
            "playback_source_candidates",
            return_value=[{"provider": "youtube", "source_id": "youtube-id"}],
        ), patch(
            "auralis_backend.discovery.source_registry.youtube_background_resolution_blocked",
            return_value=True,
        ), patch.object(stream_runtime, "proxy_stream") as proxy:
            with self.assertRaises(HTTPException):
                stream_runtime.playback_stream(SimpleNamespace(), "recording:test", request)

        proxy.assert_not_called()

    def test_background_prepare_skips_shared_youtube_block_without_marking_tracks_failed(self) -> None:
        server = _fake_server()
        server.PREPARE_SESSION_MAX_LOOKAHEAD = 8
        request = SimpleNamespace(
            track_keys=["recording:test"],
            current_track_key=None,
            active_queue=False,
            lookahead=1,
            background=True,
        )
        try:
            with patch(
                "auralis_backend.discovery.source_registry.youtube_background_resolution_blocked",
                return_value=True,
            ), patch(
                "auralis_backend.api.stream_runtime.prepare_playback_tracks"
            ) as prepare:
                response = media_runtime.prepare_session(server, request)
        finally:
            server.stream_warm_executor.shutdown(wait=True)

        prepare.assert_not_called()
        self.assertTrue(response["background_skipped"])
        self.assertEqual(response["failed"], {})

    def test_user_requested_prepare_ignores_background_youtube_block(self) -> None:
        server = _fake_server()
        server.PREPARE_SESSION_MAX_LOOKAHEAD = 8
        request = SimpleNamespace(
            track_keys=["recording:test"],
            current_track_key=None,
            active_queue=False,
            lookahead=1,
            background=False,
        )
        try:
            with patch(
                "auralis_backend.discovery.source_registry.youtube_background_resolution_blocked",
                return_value=True,
            ), patch(
                "auralis_backend.api.stream_runtime.prepare_playback_tracks",
                return_value=({"recording:test": {"prepared": True}}, {}),
            ) as prepare:
                response = media_runtime.prepare_session(server, request)
        finally:
            server.stream_warm_executor.shutdown(wait=True)

        prepare.assert_called_once()
        self.assertFalse(response["background_skipped"])
        self.assertIn("recording:test", response["prepared"])

    def test_user_requested_prepare_returns_a_ready_lead_prefix(self) -> None:
        server = _fake_server()

        def fake_extract(_server, video_id: str):
            return {
                "url": f"https://example.test/{video_id}.m4a",
                "headers": {},
                "mime_type": "audio/mp4",
                "duration": 180,
            }

        def fake_warm(_server, video_id: str, _stream_info: dict, _target_bytes: int):
            return {
                "bytes": b"x" * 128,
                "content_type": "audio/mp4",
                "total_length": 1024,
            }

        try:
            with patch.object(stream_core_runtime, "extract_stream_info", fake_extract), patch.object(
                stream_core_runtime,
                "warm_initial_stream_chunk",
                fake_warm,
            ):
                prepared, failed = stream_core_runtime.prepare_streams_with_failures(
                    server,
                    ["lead_track", "next_track"],
                    limit=2,
                    current_video_id="lead_track",
                    active_queue=True,
                )
        finally:
            server.stream_warm_executor.shutdown(wait=True)

        self.assertEqual(failed, {})
        self.assertTrue(prepared["lead_track"]["prepared"])
        self.assertFalse(prepared["lead_track"]["chunk_deferred"])
        self.assertGreater(prepared["lead_track"]["cached_prefix_bytes"], 0)
        self.assertFalse(prepared["next_track"]["chunk_deferred"])
        self.assertGreater(prepared["next_track"]["cached_prefix_bytes"], 0)

    def test_small_prefix_target_is_not_expanded_to_legacy_global_size(self) -> None:
        server = _fake_server()
        observed_ranges: list[str] = []

        response = MagicMock()
        response.headers = {
            "content-type": "audio/mp4",
            "content-range": "bytes 0-131071/1048576",
        }
        response.iter_content.return_value = [b"x" * 131072]
        response.raise_for_status.return_value = None
        server.upstream_http = MagicMock()
        server.upstream_http.get.return_value = response
        server.upstream_http.get.side_effect = lambda _url, **kwargs: (
            observed_ranges.append(kwargs["headers"]["range"]) or response
        )
        try:
            payload = stream_core_runtime.warm_initial_stream_chunk(
                server,
                "small-prefix",
                {"url": "https://example.test/audio", "headers": {}},
                131072,
            )
        finally:
            server.stream_warm_executor.shutdown(wait=True)

        self.assertEqual(observed_ranges, ["bytes=0-131071"])
        self.assertEqual(len(payload["bytes"]), 131072)

    def test_r2_head_result_is_reused_for_prepare_and_playback(self) -> None:
        cache = object.__new__(stream_cache.R2StreamCache)
        cache.bucket = "test-bucket"
        cache.prefix = "streams"
        cache._head_cache = {}
        cache._head_cache_lock = Lock()
        cache._head_hit_ttl = 45.0
        cache._head_miss_ttl = 8.0
        cache._client = MagicMock()
        cache._client.head_object.return_value = {
            "ContentLength": 1048576,
            "ContentType": "audio/mp4",
            "ETag": '"etag"',
            "Metadata": {},
        }

        first = cache.head("youtube-id")
        second = cache.head("youtube-id")

        self.assertEqual(first, second)
        self.assertEqual(cache._client.head_object.call_count, 1)

    def test_signed_stream_resolution_is_persisted_until_before_url_expiry(self) -> None:
        server = _fake_server()
        signed_expiry = int(time.time()) + 3600
        persisted = MagicMock()
        try:
            with patch.object(
                stream_core_runtime,
                "_load_persisted_stream_info",
                return_value=None,
            ), patch.object(
                stream_core_runtime,
                "extract_stream_info",
                return_value={
                    "url": f"https://example.test/audio?expire={signed_expiry}",
                    "headers": {},
                    "mime_type": "audio/mp4",
                    "duration": 180,
                },
            ), patch.object(
                stream_core_runtime,
                "_persist_stream_info",
                persisted,
            ):
                result = stream_core_runtime.get_stream_info(server, "signed-track")
        finally:
            server.stream_warm_executor.shutdown(wait=True)

        persisted.assert_called_once()
        stored_expiry = persisted.call_args.args[3]
        self.assertLess(stored_expiry, signed_expiry)
        self.assertGreater(stored_expiry, time.time())
        self.assertEqual(result["expires_at"], stored_expiry)

    def test_persisted_stream_resolution_avoids_ytdlp_after_memory_reset(self) -> None:
        server = _fake_server()
        expires_at = time.time() + 1800
        payload = {
            "url": "https://example.test/persisted-audio",
            "headers": {},
            "mime_type": "audio/mp4",
            "duration": 180,
            "expires_at": expires_at,
        }
        try:
            with patch.object(
                stream_core_runtime,
                "_load_persisted_stream_info",
                return_value=(payload, expires_at),
            ), patch.object(
                stream_core_runtime,
                "extract_stream_info",
            ) as extract:
                result = stream_core_runtime.get_stream_info(
                    server,
                    "persisted-track",
                )
        finally:
            server.stream_warm_executor.shutdown(wait=True)

        extract.assert_not_called()
        self.assertEqual(result["url"], payload["url"])
        self.assertIn("persisted-track", server.stream_info_cache)

    def test_refresh_removes_persisted_resolution_before_extracting_again(self) -> None:
        server = _fake_server()
        server.stream_info_cache["refresh-track"] = {
            "payload": {"url": "https://example.test/stale"},
            "expires_at": time.time() + 1800,
        }
        deleted = MagicMock()
        try:
            with patch.object(
                stream_core_runtime,
                "_delete_persisted_stream_info",
                deleted,
            ), patch.object(
                stream_core_runtime,
                "_load_persisted_stream_info",
                return_value=None,
            ), patch.object(
                stream_core_runtime,
                "extract_stream_info",
                return_value={
                    "url": "https://example.test/fresh",
                    "headers": {},
                    "mime_type": "audio/mp4",
                    "duration": 180,
                },
            ):
                result = stream_core_runtime.refresh_stream_info(
                    server,
                    "refresh-track",
                )
        finally:
            server.stream_warm_executor.shutdown(wait=True)

        deleted.assert_called_once_with(server, "refresh-track")
        self.assertEqual(result["url"], "https://example.test/fresh")

    def test_background_prepare_defers_every_chunk_after_resolution(self) -> None:
        server = _fake_server()

        def fake_extract(_server, video_id: str):
            return {
                "url": f"https://example.test/{video_id}.m4a",
                "headers": {},
                "mime_type": "audio/mp4",
                "duration": 180,
            }

        def fake_warm(_server, _video_id: str, _stream_info: dict, _target_bytes: int):
            return {
                "bytes": b"x" * 128,
                "content_type": "audio/mp4",
                "total_length": 1024,
            }

        try:
            with patch.object(
                stream_core_runtime,
                "extract_stream_info",
                fake_extract,
            ), patch.object(
                stream_core_runtime,
                "warm_initial_stream_chunk",
                fake_warm,
            ):
                prepared, failed = stream_core_runtime.prepare_streams_with_failures(
                    server,
                    ["lead_track", "next_track"],
                    limit=2,
                    current_video_id="lead_track",
                    active_queue=False,
                    defer_all_chunks=True,
                )
        finally:
            server.stream_warm_executor.shutdown(wait=True)

        self.assertEqual(failed, {})
        self.assertTrue(prepared["lead_track"]["chunk_deferred"])
        self.assertTrue(prepared["next_track"]["chunk_deferred"])
        self.assertEqual(prepared["lead_track"]["chunk_ms"], 0)
        self.assertEqual(prepared["next_track"]["chunk_ms"], 0)

    def test_source_blocked_failure_is_cooled_down(self) -> None:
        server = _fake_server()
        calls = {"count": 0}

        def blocked_extract(_server, _video_id: str):
            calls["count"] += 1
            raise RuntimeError("Sign in to confirm you’re not a bot")

        with patch.object(stream_core_runtime, "extract_stream_info", blocked_extract):
            prepared, failed = stream_core_runtime.prepare_streams_with_failures(
                server,
                ["blocked_track", "blocked_track"],
                limit=2,
                current_video_id="blocked_track",
                active_queue=True,
            )
            prepared_again, failed_again = stream_core_runtime.prepare_streams_with_failures(
                server,
                ["blocked_track"],
                limit=1,
                current_video_id="blocked_track",
                active_queue=True,
            )

        self.assertEqual(prepared, {})
        self.assertEqual(prepared_again, {})
        self.assertEqual(failed["blocked_track"]["code"], "source_blocked")
        self.assertEqual(failed_again["blocked_track"]["code"], "source_blocked")
        self.assertEqual(calls["count"], 1)


if __name__ == "__main__":
    unittest.main()
