from __future__ import annotations

import pathlib
import sys
import unittest

CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.domain.catalog import (
    catalog_thumbnail_url,
    catalog_source_authority,
    catalog_artist_graph_keys,
    canonical_source_identity,
    canonical_album_identity,
    canonical_artist_identity,
    canonical_title_artist_identity,
    normalized_album_payload,
    normalized_audio_traits,
    normalized_popularity,
    parse_compact_number,
    normalize_catalog_language,
    normalize_catalog_region,
    normalize_album_title,
    normalize_artist_name,
    normalize_track_title,
    normalized_track_payload,
    verified_playback_source,
)


class CatalogNormalizationTests(unittest.TestCase):
    def test_entity_thumbnail_does_not_treat_album_id_as_video_id(self) -> None:
        album = normalized_album_payload(
            {
                "id": "MPREb_album_browse_id",
                "title": "Real Album",
                "artist": "Real Artist",
                "thumbnails": [
                    {"url": "https://example.test/album.jpg"},
                ],
            }
        )
        self.assertEqual(
            album.get("thumbnail"),
            "https://example.test/album.jpg",
        )
        self.assertNotIn("i.ytimg.com", str(album.get("thumbnail") or ""))

    def test_track_thumbnail_uses_verified_video_identity(self) -> None:
        self.assertEqual(
            catalog_thumbnail_url(
                {
                    "videoId": "abcdefghijk",
                    "title": "Track",
                    "artist": "Artist",
                },
                entity_type="track",
            ),
            "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
        )

    def test_provider_label_and_generic_id_are_not_playback_proof(self) -> None:
        payload = {
            "id": "the-trooper",
            "title": "The Trooper",
            "artist": "Iron Maiden",
            "provider": "ytmusic",
        }
        self.assertEqual(verified_playback_source(payload), {})
        self.assertEqual(catalog_thumbnail_url(payload, entity_type="track"), "")

    def test_explicit_youtube_playback_source_is_preserved(self) -> None:
        payload = normalized_track_payload(
            {
                "id": "recording:canonical",
                "videoId": "abcdefghijk",
                "title": "The Trooper",
                "artist": "Iron Maiden",
            }
        )
        self.assertEqual(payload["playback_source_id"], "abcdefghijk")
        self.assertEqual(payload["playback"]["provider"], "youtube")
        self.assertEqual(
            payload["thumbnail"],
            "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
        )

    def test_compact_provider_counts_are_parsed_without_crashing(self) -> None:
        self.assertEqual(parse_compact_number("301M"), 301_000_000.0)
        self.assertEqual(parse_compact_number("1.2B views"), 1_200_000_000.0)
        self.assertEqual(parse_compact_number("845K"), 845_000.0)
        self.assertEqual(parse_compact_number("unknown"), 0.0)

        payload = normalized_track_payload(
            {
                "id": "compact-views",
                "title": "Popular Song",
                "artist": "Band",
                "views": "301M",
            }
        )
        self.assertGreater(payload["popularity"], 0.9)

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

    def test_latin_script_without_metadata_does_not_default_to_english(self) -> None:
        payload = normalized_track_payload(
            {
                "id": "track_unknown_language",
                "title": "Excitin",
                "channel": "RG Ironic",
            }
        )
        self.assertEqual(payload["language"], "unknown")
        self.assertEqual(payload["region"], "unknown")
        self.assertEqual(payload["language_confidence"], 0.0)
        self.assertEqual(payload["language_source"], "unknown")

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
        self.assertIn("band", payload["artist_graph_keys"])
        self.assertEqual(
            payload["canonical_source_identity"],
            "youtube music:track_2",
        )

    def test_release_metadata_accepts_upload_date_and_popularity_from_views(self) -> None:
        payload = normalized_track_payload(
            {
                "id": "track_3",
                "title": "Upload Date Song",
                "artist": "Band",
                "upload_date": "20240203",
                "view_count": 1000000,
            }
        )
        self.assertEqual(payload["release_date"], "2024-02-03")
        self.assertEqual(payload["release_year"], 2024)
        self.assertGreater(payload["popularity"], 0.6)
        self.assertLessEqual(payload["popularity"], 1.0)

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

    def test_audio_traits_and_artist_graph_can_be_inferred_from_metadata(self) -> None:
        traits = normalized_audio_traits(
            {
                "title": "High Energy Rock Anthem",
                "artist": "Band",
                "description": "official upbeat guitar workout song",
            }
        )
        self.assertGreaterEqual(traits["energy"], 0.7)
        self.assertGreaterEqual(traits["drive"], 0.7)
        self.assertEqual(
            catalog_artist_graph_keys(
                {
                    "artist": "Prince",
                    "artists": [{"name": "The Revolution"}],
                    "related_artists": ["Sheila E."],
                }
            ),
            ["prince", "the revolution", "sheila e"],
        )
        self.assertGreater(normalized_popularity({"views": 100000}), 0.5)

    def test_catalog_payload_infers_broad_genre_traits_and_region(self) -> None:
        payload = normalized_track_payload(
            {
                "id": "rock_track",
                "title": "Arena Rock Workout Anthem",
                "artist": "Dio",
                "description": "classic rock and heavy metal song for the gym",
                "provider": "youtube_music",
            }
        )
        self.assertEqual(payload["genre"], "rock")
        self.assertIn("classic_rock", payload["discovery_genres"])
        self.assertIn("heavy_metal", payload["discovery_genres"])
        self.assertGreaterEqual(payload["audio_traits"]["energy"], 0.8)
        self.assertGreaterEqual(payload["audio_traits"]["drive"], 0.8)
        self.assertEqual(payload["source_authority"], "verified_catalog")

    def test_catalog_payload_infers_non_english_region_without_blocking_global(self) -> None:
        payload = normalized_track_payload(
            {
                "id": "latin_track",
                "title": "Latin Pop Amor",
                "artist": "Artist",
                "description": "reggaeton en espanol",
            }
        )
        self.assertEqual(payload["genre"], "latin")
        self.assertEqual(payload["language"], "spanish")
        self.assertEqual(payload["region"], "latin_america")

    def test_language_and_region_aliases_normalize_to_stable_keys(self) -> None:
        self.assertEqual(normalize_catalog_language("EN"), "english")
        self.assertEqual(normalize_catalog_language("Vi"), "vietnamese")
        self.assertEqual(normalize_catalog_region("United States"), "us")
        self.assertEqual(normalize_catalog_region("Latin America"), "latin_america")

    def test_provider_backed_track_with_title_artist_is_verified_catalog(self) -> None:
        payload = normalized_track_payload(
            {
                "id": "yt_track",
                "title": "Purple Rain",
                "artist": "Prince & The Revolution",
                "provider": "youtube_music",
            }
        )
        self.assertEqual(payload["source_authority"], "verified_catalog")

    def test_cafe_background_sources_are_search_only(self) -> None:
        self.assertEqual(
            catalog_source_authority(
                {
                    "id": "cafe",
                    "title": "Coffee Shop Music Relax Jazz",
                    "artist": "Restaurant Background Music",
                }
            ),
            "search_only",
        )

    def test_generic_cover_sources_are_search_only_without_artist_blacklists(self) -> None:
        self.assertEqual(
            catalog_source_authority(
                {
                    "id": "cover-channel",
                    "title": "Bring Me To Life",
                    "artist": "Studio Cover Band",
                    "description": "Instrumental cover version",
                }
            ),
            "search_only",
        )
        self.assertNotEqual(
            catalog_source_authority(
                {
                    "id": "artist-channel",
                    "title": "Bring Me To Life",
                    "artist": "Cole Rolland",
                }
            ),
            "search_only",
        )


if __name__ == "__main__":
    unittest.main()
