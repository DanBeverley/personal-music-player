from __future__ import annotations

import pathlib
import sys
import unittest

CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.domain.catalog import (
    catalog_source_authority,
    canonical_source_identity,
    canonical_album_identity,
    canonical_artist_identity,
    canonical_title_artist_identity,
    normalized_album_payload,
    normalized_audio_traits,
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

    def test_normalized_payload_preserves_release_region_popularity_and_audio_traits(self) -> None:
        payload = normalized_track_payload(
            {
                "id": "track_2",
                "title": "Fast Song",
                "artist": "Band",
                "album": "Album",
                "release_date": "2024-03-15",
                "language": "English",
                "region": "US",
                "popularity": 0.82,
                "mood_axes": {"energy": 0.91, "drive": 0.84},
                "provider": "youtube_music",
            }
        )
        self.assertEqual(payload["release_year"], 2024)
        self.assertEqual(payload["language"], "english")
        self.assertEqual(payload["region"], "us")
        self.assertEqual(payload["popularity"], 0.82)
        self.assertEqual(payload["audio_traits"]["energy"], 0.91)
        self.assertEqual(
            payload["canonical_source_identity"],
            "youtube music:track_2",
        )

    def test_source_authority_separates_official_catalog_and_search_only_media(self) -> None:
        self.assertEqual(
            catalog_source_authority(
                {
                    "id": "official",
                    "title": "Song",
                    "artist": "Band - Topic",
                }
            ),
            "official",
        )
        self.assertEqual(
            catalog_source_authority(
                {
                    "id": "cover",
                    "title": "Song Acoustic Cover",
                    "artist": "Tribute Band",
                }
            ),
            "search_only",
        )

    def test_album_payload_has_stable_canonical_source_identity(self) -> None:
        payload = normalized_album_payload(
            {
                "id": "album_1",
                "title": "Purple Rain",
                "artist": "Prince",
                "source_name": "artist_discography",
            }
        )
        self.assertEqual(
            payload["canonical_source_identity"],
            "artist discography:album_1",
        )
        self.assertEqual(
            normalized_audio_traits({"energy": 2.0, "softness": -1.0}),
            {"energy": 1.0, "softness": 0.0},
        )


if __name__ == "__main__":
    unittest.main()
