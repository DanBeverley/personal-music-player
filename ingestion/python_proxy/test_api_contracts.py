from __future__ import annotations

import pathlib
import sys
from typing import get_type_hints
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.api import routes
from auralis_backend.contracts import (
    AssistantChatRequest,
    AssistantSessionCreateRequest,
    DownloadRequest,
    SearchRequest,
    WarmStreamRequest,
)


class _FakeServer:
    RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS = 24


class _FakeSearchService:
    def __init__(self) -> None:
        self.last_search_request = None

    def search(self, req):
        self.last_search_request = req
        return {
            "status": "success",
            "request_id": "search-req-1",
            "model_version": "search:test",
            "query_intent": "track",
            "top_result": {
                "entity_type": "track",
                "item": {
                    "id": "track_1",
                    "title": "Black Dog",
                    "channel": "Led Zeppelin",
                },
            },
            "tracks": [
                {
                    "id": "track_1",
                    "title": "Black Dog",
                    "channel": "Led Zeppelin",
                }
            ],
            "artists": [{"id": "artist_1", "name": "Led Zeppelin"}],
            "albums": [{"id": "album_1", "title": "Led Zeppelin IV"}],
            "similar_artists": [{"id": "artist_2", "name": "The Who"}],
            "diagnostics": {"contract_test": True},
        }


class _FakeRecommendationService:
    def __init__(self) -> None:
        self.last_recommend_request = None

    def recommend(self, req):
        self.last_recommend_request = req
        if req.session_id and req.row_id:
          return {
              "status": "success",
              "request_id": "recommend-row-page-1",
              "session_id": req.session_id,
              "generated_at": 1713000000.0,
              "expires_at": 1713003600.0,
              "model_version": "home:test",
              "rows": [
                  {
                      "id": req.row_id,
                      "title": "Because you played",
                      "kind": req.row_id,
                      "item_type": "track",
                      "items": [
                          {
                              "id": "track_2",
                              "title": "Rock and Roll",
                              "channel": "Led Zeppelin",
                          }
                      ],
                      "next_offset": req.offset + req.limit,
                      "has_more": True,
                  }
              ],
              "diagnostics": {"contract_test": True, "request_mode": "row_page"},
          }
        return {
            "status": "success",
            "request_id": "recommend-feed-1",
            "session_id": "session_1",
            "generated_at": 1713000000.0,
            "expires_at": 1713003600.0,
            "model_version": "home:test",
            "rows": [
                {
                    "id": "continue_listening",
                    "title": "Continue listening",
                    "kind": "continue_listening",
                    "item_type": "track",
                    "items": [
                        {
                            "id": "track_1",
                            "title": "Black Dog",
                            "channel": "Led Zeppelin",
                        }
                    ],
                    "next_offset": 1,
                    "has_more": False,
                }
            ],
            "diagnostics": {"contract_test": True, "request_mode": "full_feed"},
        }


class _FakeAssistantService:
    def __init__(self) -> None:
        self.last_chat_request = None
        self.last_create_request = None

    def list_sessions(self, user_scope_id: str, *, include_archived: bool = False):
        return {
            "status": "success",
            "sessions": [
                {
                    "id": "assistant_session_1",
                    "title": "Rock queue",
                    "user_id": user_scope_id,
                    "archived_at": None,
                    "pinned_at": None,
                }
            ],
        }

    def create_session(self, req):
        self.last_create_request = req
        return {
            "status": "success",
            "session": {
                "id": "assistant_session_2",
                "title": req.title or "New Session",
            },
        }

    def get_session(self, session_id: str, user_scope_id: str):
        return {
            "status": "success",
            "session": {
                "id": session_id,
                "title": "Rock queue",
                "user_id": user_scope_id,
            },
            "messages": [],
        }

    def update_session(self, session_id: str, req):
        return {
            "status": "success",
            "session": {
                "id": session_id,
                "title": req.title or "Rock queue",
            },
        }

    def delete_session(self, session_id: str, user_scope_id: str):
        return {"status": "success"}

    def chat(self, req):
        self.last_chat_request = req
        return {
            "status": "success",
            "mode": "conversation",
            "reply": "Try Houses of the Holy next.",
            "session_id": "assistant_session_1",
            "session_title": "Rock queue",
            "session": {"id": "assistant_session_1", "title": "Rock queue"},
            "tracks": [],
            "playlist_draft": None,
            "target_playlist": None,
            "playlist_options": [],
            "fact_cards": [],
            "source_links": [],
            "clarification_options": [],
        }


class _FakeMediaService:
    def __init__(self) -> None:
        self.last_prepare_request = None
        self.last_track_details_request = None

    def health_check(self):
        return {"status": "Auralis Python Proxy is running"}

    def latency_summary(self):
        return {"status": "success", "summary": {}}

    def prepare_session(self, req):
        self.last_prepare_request = req
        return {
            "status": "success",
            "prepared": {"track_1": {"resolve_ms": 12}},
            "failed": {},
        }

    def get_track_details(self, req):
        self.last_track_details_request = req
        return {"status": "success", "video_id": req.video_id}

    def get_track_lyrics(self, video_id: str):
        return {
            "status": "success",
            "video_id": video_id,
            "has_lyrics": False,
            "has_timestamps": False,
            "source": None,
            "lines": [],
        }

    def get_album_details(self, album_id: str):
        return {"status": "success", "id": album_id, "tracks": []}

    def get_artist_details(self, artist_id: str):
        return {"status": "success", "id": artist_id, "top_songs": []}


class _FakeRecognitionService:
    def __init__(self) -> None:
        self.last_content_type = ""

    async def recognize_audio(self, request):
        self.last_content_type = request.headers.get("content-type", "")
        return {
            "status": "success",
            "request_id": "recognize-1",
            "recognition_status": "resolved",
            "provider": "acrcloud",
            "confidence": 98.0,
            "recognized_metadata": {
                "title": "Stairway to Heaven",
                "artist": "Led Zeppelin",
            },
            "resolved_track": {
                "id": "track_1",
                "title": "Stairway to Heaven",
                "channel": "Led Zeppelin",
            },
            "alternatives": [],
            "diagnostics": {"contract_test": True},
        }


class ApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_server = routes._server
        self._original_search_service = routes._search_service
        self._original_recommendation_service = routes._recommendation_service
        self._original_assistant_service = routes._assistant_service
        self._original_media_service = routes._media_service
        self._original_recognition_service = routes._recognition_service

        self.fake_server = _FakeServer()
        self.fake_search_service = _FakeSearchService()
        self.fake_recommendation_service = _FakeRecommendationService()
        self.fake_assistant_service = _FakeAssistantService()
        self.fake_media_service = _FakeMediaService()
        self.fake_recognition_service = _FakeRecognitionService()

        routes._server = self.fake_server
        routes._search_service = self.fake_search_service
        routes._recommendation_service = self.fake_recommendation_service
        routes._assistant_service = self.fake_assistant_service
        routes._media_service = self.fake_media_service
        routes._recognition_service = self.fake_recognition_service

        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        routes._server = self._original_server
        routes._search_service = self._original_search_service
        routes._recommendation_service = self._original_recommendation_service
        routes._assistant_service = self._original_assistant_service
        routes._media_service = self._original_media_service
        routes._recognition_service = self._original_recognition_service

    def test_search_request_accepts_search_mode(self) -> None:
        req = SearchRequest(query="billie jean", search_mode="entity")
        self.assertEqual(req.search_mode, "entity")

    def test_router_uses_backend_contract_models(self) -> None:
        search_hints = get_type_hints(routes.search)
        recommend_hints = get_type_hints(routes.recommend)
        assistant_chat_hints = get_type_hints(routes.assistant_chat)
        assistant_create_hints = get_type_hints(routes.assistant_create_session)
        prepare_hints = get_type_hints(routes.prepare_session)
        warm_hints = get_type_hints(routes.warm_streams)
        download_hints = get_type_hints(routes.download_audio)

        self.assertIs(search_hints["req"], SearchRequest)
        self.assertIs(recommend_hints["req"], SearchRequest)
        self.assertIs(
            assistant_chat_hints["req"],
            AssistantChatRequest,
        )
        self.assertIs(
            assistant_create_hints["req"],
            AssistantSessionCreateRequest,
        )
        self.assertIs(prepare_hints["req"], WarmStreamRequest)
        self.assertIs(warm_hints["req"], WarmStreamRequest)
        self.assertIs(download_hints["req"], DownloadRequest)

    def test_search_endpoint_contract_matches_flutter_payload_shape(self) -> None:
        response = self.client.post(
            "/search",
            json={
                "query": "black dog",
                "user_scope_id": "guest",
                "limit": 8,
                "recent_queries": ["classic rock"],
                "taste_queries": ["hard rock"],
                "recent_track_ids": ["track_1"],
                "top_track_ids": ["track_2"],
                "recent_track_snapshots": [
                    {"id": "track_1", "title": "Black Dog", "channel": "Led Zeppelin"}
                ],
                "top_track_snapshots": [
                    {
                        "id": "track_2",
                        "title": "Whole Lotta Love",
                        "channel": "Led Zeppelin",
                    }
                ],
                "last_played_tracks": [
                    {"id": "track_3", "title": "Immigrant Song", "channel": "Led Zeppelin"}
                ],
                "playlist_names": ["Road trip"],
                "library_track_ids": ["track_4"],
                "offline_track_ids": ["track_4"],
                "artist_hints": ["Led Zeppelin"],
                "album_hints": ["Led Zeppelin IV"],
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertIsInstance(self.fake_search_service.last_search_request, SearchRequest)
        self.assertEqual(
            "track_1",
            self.fake_search_service.last_search_request.recent_track_snapshots[0]["id"],
        )
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertIn("tracks", payload)
        self.assertIn("artists", payload)
        self.assertIn("albums", payload)
        self.assertIn("similar_artists", payload)
        self.assertIn("diagnostics", payload)

    def test_recognize_audio_endpoint_accepts_multipart_uploads(self) -> None:
        response = self.client.post(
            "/recognize_audio",
            data={
                "source_type": "uploaded",
                "media_kind": "audio",
                "user_scope_id": "guest",
                "filename": "snippet.wav",
            },
            files={
                "media": ("snippet.wav", b"RIFF....WAVEfmt ", "audio/wav"),
            },
        )
        self.assertEqual(200, response.status_code)
        self.assertIn("multipart/form-data", self.fake_recognition_service.last_content_type)
        payload = response.json()
        self.assertEqual("resolved", payload["recognition_status"])
        self.assertEqual("track_1", payload["resolved_track"]["id"])

    def test_recommend_endpoint_accepts_slim_home_feed_payload(self) -> None:
        response = self.client.post(
            "/recommend",
            json={
                "query": "",
                "user_scope_id": "guest",
                "limit": 8,
                "offset": 0,
                "force_refresh": True,
                "prefer_fresh_rows": True,
                "refresh_token": "manual-refresh-1",
                "seed_id": "track_1",
                "seed_ids": ["track_1", "track_2"],
                "recent_track_ids": ["track_1"],
                "top_track_ids": ["track_2"],
                "recent_track_snapshots": [
                    {"id": "track_1", "title": "Black Dog", "channel": "Led Zeppelin"}
                ],
                "top_track_snapshots": [
                    {
                        "id": "track_2",
                        "title": "Whole Lotta Love",
                        "channel": "Led Zeppelin",
                    }
                ],
                "last_played_tracks": [
                    {"id": "track_3", "title": "Immigrant Song", "channel": "Led Zeppelin"}
                ],
                "recent_queries": ["classic rock"],
                "playlist_names": ["Road trip"],
                "library_track_ids": ["track_4"],
                "offline_track_ids": ["track_4"],
                "artist_hints": ["Led Zeppelin"],
                "taste_queries": ["hard rock"],
                "avoid_ids": ["track_9"],
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertIsInstance(
            self.fake_recommendation_service.last_recommend_request,
            SearchRequest,
        )
        req = self.fake_recommendation_service.last_recommend_request
        self.assertEqual(["Led Zeppelin"], req.artist_hints)
        self.assertEqual(["hard rock"], req.taste_queries)
        self.assertEqual(["track_4"], req.library_track_ids)
        self.assertEqual(["track_4"], req.offline_track_ids)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual("session_1", payload["session_id"])
        self.assertIn("rows", payload)
        self.assertIn("diagnostics", payload)

    def test_recommend_row_page_contract_is_preserved(self) -> None:
        response = self.client.post(
            "/recommend",
            json={
                "query": "",
                "user_scope_id": "guest",
                "session_id": "session_existing",
                "row_id": "because_you_played",
                "offset": 8,
                "limit": 4,
            },
        )

        self.assertEqual(200, response.status_code)
        req = self.fake_recommendation_service.last_recommend_request
        self.assertEqual("session_existing", req.session_id)
        self.assertEqual("because_you_played", req.row_id)
        self.assertEqual(8, req.offset)
        self.assertEqual(4, req.limit)
        payload = response.json()
        self.assertEqual("session_existing", payload["session_id"])
        self.assertEqual("because_you_played", payload["rows"][0]["id"])

    def test_assistant_chat_endpoint_contract_matches_current_payload_shape(self) -> None:
        response = self.client.post(
            "/assistant/chat",
            json={
                "message": "Give me something like Led Zeppelin",
                "user_scope_id": "guest",
                "session_id": "assistant_session_1",
                "conversation": [{"role": "user", "content": "Play rock"}],
                "last_assistant_tracks": [
                    {"id": "track_1", "title": "Black Dog", "artist": "Led Zeppelin"}
                ],
                "recent_assistant_tracks": [
                    {"id": "track_2", "title": "Kashmir", "artist": "Led Zeppelin"}
                ],
                "playlist_summaries": [
                    {"id": "playlist_1", "name": "Rock", "track_count": 12}
                ],
                "recent_track_ids": ["track_1"],
                "recent_queries": ["classic rock"],
                "library_tracks": [
                    {"id": "track_3", "title": "Immigrant Song", "artist": "Led Zeppelin"}
                ],
                "limit": 6,
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertIsInstance(
            self.fake_assistant_service.last_chat_request,
            AssistantChatRequest,
        )
        self.assertEqual(
            "assistant_session_1",
            self.fake_assistant_service.last_chat_request.session_id,
        )
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual("assistant_session_1", payload["session_id"])
        self.assertIn("reply", payload)
        self.assertIn("session", payload)

    def test_assistant_session_create_contract_is_preserved(self) -> None:
        response = self.client.post(
            "/assistant/sessions",
            json={
                "user_scope_id": "guest",
                "title": "Late night mix",
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertIsInstance(
            self.fake_assistant_service.last_create_request,
            AssistantSessionCreateRequest,
        )
        self.assertEqual("Late night mix", self.fake_assistant_service.last_create_request.title)
        self.assertEqual("assistant_session_2", response.json()["session"]["id"])

    def test_stream_endpoint_contracts_accept_current_payloads(self) -> None:
        captured = {}

        def fake_warm_streams(server, req):
            captured["warm_server"] = server
            captured["warm_req"] = req
            return {"status": "success", "queued": list(req.video_ids)}

        def fake_download(server, req):
            captured["download_server"] = server
            captured["download_req"] = req
            return {"status": "success", "video_id": req.video_id}

        with patch.object(routes, "warm_streams_runtime", side_effect=fake_warm_streams), patch.object(
            routes,
            "download_audio_runtime",
            side_effect=fake_download,
        ):
            prepare_response = self.client.post(
                "/prepare_session",
                json={
                    "video_ids": ["track_1", "track_2"],
                    "current_video_id": "track_1",
                    "active_queue": True,
                    "lookahead": 2,
                },
            )
            warm_response = self.client.post(
                "/warm_streams",
                json={
                    "video_ids": ["track_1"],
                    "current_video_id": "track_1",
                    "active_queue": False,
                    "lookahead": 1,
                },
            )
            download_response = self.client.post(
                "/download",
                json={
                    "video_id": "track_1",
                    "title": "Black Dog",
                },
            )

        self.assertEqual(200, prepare_response.status_code)
        self.assertEqual(200, warm_response.status_code)
        self.assertEqual(200, download_response.status_code)
        self.assertIsInstance(self.fake_media_service.last_prepare_request, WarmStreamRequest)
        self.assertIsInstance(captured["warm_req"], WarmStreamRequest)
        self.assertIsInstance(captured["download_req"], DownloadRequest)
        self.assertEqual(
            ["track_1", "track_2"],
            self.fake_media_service.last_prepare_request.video_ids,
        )
        self.assertEqual("track_1", captured["download_req"].video_id)


if __name__ == "__main__":
    unittest.main()
