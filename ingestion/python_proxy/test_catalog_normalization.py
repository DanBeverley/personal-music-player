from __future__ import annotations

import pathlib
import sys
import unittest

CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.domain.catalog import (
    canonical_album_identity,
    canonical_artist_identity,
    canonical_title_artist_identity,
    normalize_album_title,
    normalize_artist_name,
    normalize_track_title,
    normalized_track_payload,
)


class CatalogNormalizationTests(unittest.TestCase):
    def test_normalize_track_title_strips_common_release_noise(self) -> None:
        self.assertEqual(
            normalize_track_title("Stairway to Heaven (Remastered 2012) - Official Audio"),
            "stairway to heaven",
        )

    def test_normalize_artist_name_strips_topic_and_feature_suffix(self) -> None:
        self.assertEqual(
            normalize_artist_name("Led Zeppelin - Topic"),
            "led zeppelin",
        )
        self.assertEqual(
            normalize_artist_name("Guns N' Roses feat. Izzy Stradlin"),
            "guns n roses",
        )

    def test_normalize_album_title_strips_deluxe_suffix(self) -> None:
        self.assertEqual(
            normalize_album_title("Led Zeppelin IV (Deluxe Edition)"),
            "led zeppelin iv",
        )

    def test_canonical_title_artist_identity_matches_variant_rows(self) -> None:
        first = canonical_title_artist_identity(
            {
                "title": "Stairway to Heaven (Remastered 2012)",
                "channel": "Led Zeppelin - Topic",
            }
        )
        second = canonical_title_artist_identity(
            {
                "title": "Stairway To Heaven",
                "artist": "Led Zeppelin",
            }
        )
        self.assertEqual(first, second)

    def test_canonical_album_identity_uses_normalized_title_artist_pair(self) -> None:
        self.assertEqual(
            canonical_album_identity(
                {
                    "title": "Led Zeppelin IV (Deluxe Edition)",
                    "artist": "Led Zeppelin - Topic",
                }
            ),
            "led zeppelin iv|led zeppelin",
        )

    def test_normalized_track_payload_includes_canonical_fields(self) -> None:
        payload = normalized_track_payload(
            {
                "id": "track_1",
                "title": "November Rain (Live)",
                "channel": "Guns N' Roses - Topic",
                "album": "Use Your Illusion I (Deluxe)",
            }
        )
        self.assertEqual(payload["normalized_title"], "november rain")
        self.assertEqual(payload["normalized_artist_name"], "guns n roses")
        self.assertEqual(payload["normalized_album_title"], "use your illusion i")
        self.assertEqual(payload["canonical_track_identity"], "track_1")

    def test_canonical_artist_identity_prefers_id_when_present(self) -> None:
        self.assertEqual(
            canonical_artist_identity({"id": "artist_1", "name": "Anything"}),
            "artist_1",
        )


if __name__ == "__main__":
    unittest.main()
