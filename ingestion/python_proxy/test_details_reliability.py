import pathlib
import sys
import unittest


CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.details.detail_runtime import build_artist_details_payload


class _FakeYTMusic:
    def get_artist(self, _artist_id):
        return {
            "name": "Primary Artist",
            "songs": {"results": [{"videoId": "song_1"}]},
            "albums": {"results": [{"browseId": "album_1"}]},
            "related": {"results": [{"browseId": "related_1", "name": "Related"}]},
        }


class _PartialFailureServer:
    DETAIL_RESULT_CACHE_TTL_SECONDS = 60
    detail_result_cache = {}
    detail_result_cache_lock = None
    ytmusic = _FakeYTMusic()

    def _recommendation_trim_text(self, value):
        return str(value or "").strip()

    def _cache_lookup(self, *_args):
        return None

    def _cache_store(self, *_args):
        return None

    def _normalize_artist_song_entries(self, *_args, **_kwargs):
        raise RuntimeError("songs unavailable")

    def _normalize_artist_album_entries(self, *_args, **_kwargs):
        raise RuntimeError("albums unavailable")

    def normalize_artist_results(self, results):
        return [{"id": "related_1", "name": "Related"}] if results else []

    def _rank_artist_detail_related_artists(self, *_args, **_kwargs):
        raise RuntimeError("related ranking unavailable")

    def _summarize_artist_description(self, description):
        return description

    def _normalize_artist_stats(self, _artist):
        raise RuntimeError("stats unavailable")

    def extract_thumbnail(self, _payload):
        raise RuntimeError("thumbnail unavailable")


class DetailReliabilityTests(unittest.TestCase):
    def test_artist_details_preserve_partial_payload_when_optional_sections_fail(self) -> None:
        payload = build_artist_details_payload(
            _PartialFailureServer(),
            "artist_1",
        )

        self.assertEqual(payload.get("status"), "success")
        self.assertEqual(payload.get("name"), "Primary Artist")
        self.assertEqual(payload.get("top_songs"), [])
        self.assertEqual(payload.get("albums"), [])
        self.assertEqual(payload.get("related_artists")[0].get("id"), "related_1")
        self.assertEqual(payload.get("stats"), {})
        self.assertIsNone(payload.get("thumbnail"))


if __name__ == "__main__":
    unittest.main()
