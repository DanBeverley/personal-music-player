from __future__ import annotations

import pathlib
import sys
import unittest

CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.recommend.evaluation import (
    artifact_repetition_reasons,
    evaluate_home_fixture,
)


class RecommendationFixtureEvaluationTests(unittest.TestCase):
    def test_fixture_evaluation_flags_duplicates_language_mismatch_and_missing_rows(self) -> None:
        metrics = evaluate_home_fixture(
            profile={
                "supported_languages": ["english"],
                "recent_track_snapshots": [
                    {
                        "title": "Stairway to Heaven",
                        "channel": "Led Zeppelin",
                    }
                ],
            },
            rows=[
                {
                    "kind": "continue_listening",
                    "items": [
                        {
                            "id": "track_1",
                            "title": "Stairway to Heaven (Remastered 2012)",
                            "channel": "Led Zeppelin - Topic",
                            "language": "english",
                        },
                        {
                            "id": "track_2",
                            "title": "Stairway To Heaven",
                            "artist": "Led Zeppelin",
                            "language": "english",
                        },
                    ],
                },
                {
                    "kind": "quiet_picks",
                    "items": [
                        {
                            "id": "track_3",
                            "title": "Tokyo Nights",
                            "channel": "City Pop Artist",
                            "language": "japanese",
                        }
                    ],
                },
            ],
            row_diagnostics={
                "continue_listening": {"status": "emitted"},
                "quiet_picks": {"status": "emitted"},
            },
            required_rows=(
                "continue_listening",
                "because_you_played",
                "quiet_picks",
            ),
        )
        self.assertEqual(metrics["duplicate_track_count"], 1)
        self.assertEqual(metrics["off_profile_language_count"], 1)
        self.assertEqual(metrics["recent_repeat_count"], 2)
        self.assertEqual(metrics["missing_required_rows"], ["because_you_played"])
        self.assertIn("visible_duplicate_tracks:1", metrics["reasons"])
        self.assertIn("off_profile_language_items:1", metrics["reasons"])

    def test_artifact_repetition_reasons_uses_canonical_title_artist_identity(self) -> None:
        reasons = artifact_repetition_reasons(
            [
                {
                    "kind": "continue_listening",
                    "items": [
                        {
                            "id": "track_1",
                            "title": "Sweet Child O' Mine (Remastered)",
                            "channel": "Guns N' Roses - Topic",
                        },
                        {
                            "id": "track_2",
                            "title": "Sweet Child O' Mine",
                            "artist": "Guns N' Roses",
                        },
                    ],
                },
                {
                    "kind": "because_you_played",
                    "items": [
                        {
                            "id": "track_3",
                            "title": "Paradise City",
                            "channel": "Guns N' Roses",
                        },
                        {
                            "id": "track_4",
                            "title": "Welcome to the Jungle",
                            "channel": "Guns N' Roses",
                        },
                    ],
                },
            ],
            max_visible_same_artist=3,
        )
        self.assertIn("visible_duplicate_tracks:1", reasons)
        self.assertIn("visible_artist_concentration:4", reasons)

    def test_fixture_evaluation_rejects_search_only_sources_and_unknown_album_artists(self) -> None:
        metrics = evaluate_home_fixture(
            profile={
                "supported_languages": ["english"],
                "supported_regions": ["us", "global"],
            },
            rows=[
                {
                    "kind": "made_for_you",
                    "items": [
                        {
                            "id": "tribute",
                            "title": "Rock Tribute",
                            "artist": "Tribute Band",
                            "language": "english",
                            "region": "us",
                            "source_authority": "search_only",
                        }
                    ],
                },
                {
                    "kind": "recommended_albums",
                    "items": [
                        {
                            "id": "album-without-artist",
                            "title": "Unknown Collection",
                            "album_source": "album_search",
                            "language": "spanish",
                            "region": "latin_america",
                            "source_authority": "unknown",
                        }
                    ],
                },
            ],
        )
        self.assertEqual(metrics["search_only_source_count"], 1)
        self.assertEqual(metrics["unknown_album_artist_count"], 1)
        self.assertEqual(metrics["off_profile_language_count"], 1)
        self.assertEqual(metrics["off_profile_region_count"], 1)
        self.assertIn("search_only_source_items:1", metrics["reasons"])
        self.assertIn("unknown_album_artist_items:1", metrics["reasons"])


if __name__ == "__main__":
    unittest.main()
