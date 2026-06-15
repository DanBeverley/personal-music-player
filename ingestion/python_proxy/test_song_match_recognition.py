import pathlib
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.recognition.media import choose_recognition_windows
from auralis_backend.recognition.providers.acrcloud import ACRCloudRecognitionProvider
from auralis_backend.recognition.providers.base import RecognitionMatch
from auralis_backend.recognition.service import RecognitionService


class SongMatchRecognitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.server = SimpleNamespace(
            recommendation_store_lock=threading.Lock(),
            RECOMMENDATION_STORE_DB_PATH=str(
                pathlib.Path(self._temp_dir.name) / "recognition_store.sqlite"
            ),
        )
        self.service = RecognitionService(self.server)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_choose_recognition_windows_uses_middle_bias_for_long_media(self) -> None:
        windows = choose_recognition_windows(210.0)
        self.assertEqual(3, len(windows))
        self.assertGreater(windows[0][0], 50.0)
        self.assertLess(windows[-1][0], 160.0)

    def test_acrcloud_payload_mapping_extracts_music_matches(self) -> None:
        provider = ACRCloudRecognitionProvider(
            host="example.com",
            access_key="key",
            access_secret="secret",
        )
        matches = provider._extract_matches(
            {
                "status": {"code": 0},
                "metadata": {
                    "music": [
                        {
                            "title": "Whole Lotta Love",
                            "artists": [{"name": "Led Zeppelin"}],
                            "album": {"name": "Led Zeppelin II"},
                            "score": 97,
                            "duration_ms": 333000,
                        }
                    ]
                },
            }
        )
        self.assertEqual(1, len(matches))
        self.assertEqual("Whole Lotta Love", matches[0].title)
        self.assertEqual("Led Zeppelin", matches[0].artist)
        self.assertEqual(97.0, matches[0].confidence)

    @patch("auralis_backend.recognition.service.search_tracks_direct")
    def test_resolver_prefers_official_artist_over_tribute_variant(self, mock_search_tracks_direct) -> None:
        mock_search_tracks_direct.return_value = [
            {
                "id": "official_1",
                "title": "Stairway to Heaven",
                "channel": "Led Zeppelin",
                "album": "Led Zeppelin IV",
                "duration": 482,
            },
            {
                "id": "tribute_1",
                "title": "Stairway to Heaven",
                "channel": "Led Zeppelin Tribute Band",
                "album": "A Tribute to Led Zeppelin",
                "duration": 478,
            },
        ]
        resolved = self.service._resolve_aggregated_match(
            {
                "recognized_metadata": {
                    "title": "Stairway to Heaven",
                    "artist": "Led Zeppelin",
                    "album": "Led Zeppelin IV",
                    "duration_ms": 482000,
                },
                "confidence": 96.0,
                "window_hits": 2,
                "provider_matches": [],
            }
        )
        self.assertEqual("official_1", resolved["resolved_track"]["id"])
        self.assertGreater(resolved["resolution_score"], 10.0)

    @patch("auralis_backend.recognition.service.search_tracks_direct")
    def test_aggregate_matches_rewards_repeat_agreement_across_windows(self, mock_search_tracks_direct) -> None:
        mock_search_tracks_direct.return_value = []
        matches = self.service._aggregate_matches(
            [
                (
                    {"start_seconds": 10.0, "duration_seconds": 12.0},
                    [
                        RecognitionMatch(
                            title="Lithium",
                            artist="Nirvana",
                            confidence=81.0,
                        )
                    ],
                ),
                (
                    {"start_seconds": 55.0, "duration_seconds": 12.0},
                    [
                        RecognitionMatch(
                            title="Lithium",
                            artist="Nirvana",
                            confidence=79.0,
                        )
                    ],
                ),
            ]
        )
        self.assertEqual(1, len(matches))
        self.assertEqual(2, matches[0]["window_hits"])
        self.assertGreater(matches[0]["confidence"], 80.0)


if __name__ == "__main__":
    unittest.main()
