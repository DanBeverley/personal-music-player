import pathlib
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import server
from auralis_backend.search.runtime import search_canonical_album_for_track
from auralis_backend.search.service import SearchService
from auralis_backend.domain.result_quality import album_result_penalty


class SearchEnrichmentTests(unittest.TestCase):
    def test_exact_title_results_prefer_dominant_canonical_artist(self) -> None:
        tracks = [
            {"id": "cover", "title": "Purple Rain", "channel": "Other Artist"},
            {
                "id": "canonical",
                "title": "Purple Rain",
                "channel": "Prince & The Revolution",
                "album": "Purple Rain",
            },
            {
                "id": "canonical-live",
                "title": "Purple Rain",
                "channel": "Prince & The Revolution",
            },
        ]
        ordered = SearchService(server)._canonicalize_direct_tracks(
            "Purple Rain",
            tracks,
        )
        self.assertEqual(ordered[0].get("id"), "canonical")
        self.assertEqual(ordered[-1].get("id"), "cover")

    def test_search_enrichment_excludes_unknown_artist_albums(self) -> None:
        albums = SearchService(server)._unique_albums(
            [
                {"id": "unknown", "title": "Mystery", "artist": "Unknown Artist"},
                {"id": "known", "title": "Purple Rain", "artist": "Prince"},
            ],
            8,
        )
        self.assertEqual([album.get("id") for album in albums], ["known"])

    def test_unknown_album_artist_receives_hard_quality_penalty(self) -> None:
        penalty = album_result_penalty(
            server,
            {"title": "Purple Rain", "artist": "Unknown Artist"},
            query="Purple Rain",
        )
        self.assertGreaterEqual(penalty, 5.0)

    def test_track_match_quality_accepts_title_plus_artist_query(self) -> None:
        score = SearchService(server)._direct_track_match_score(
            "The Song Primary Artist",
            {
                "id": "track_1",
                "title": "The Song",
                "channel": "Primary Artist",
            },
        )

        self.assertGreaterEqual(score, 0.9)

    @patch("auralis_backend.search.runtime.search_albums_direct")
    def test_canonical_album_rejects_same_title_from_wrong_artist(
        self,
        mock_search_albums,
    ) -> None:
        mock_search_albums.return_value = [
            {"id": "wrong", "title": "Greatest Hits", "artist": "Other Artist"},
            {"id": "right", "title": "Greatest Hits", "artist": "Primary Artist"},
        ]

        album = search_canonical_album_for_track(
            {
                "id": "track_1",
                "title": "The Song",
                "channel": "Primary Artist",
                "album": "Greatest Hits",
            },
            server=server,
        )

        self.assertEqual((album or {}).get("id"), "right")

    @patch.object(SearchService, "_track_similar_tracks")
    @patch.object(server, "_build_artist_details_payload")
    @patch("auralis_backend.search.service.search_canonical_album_for_track")
    @patch("auralis_backend.search.service.search_artists_direct_cached")
    def test_enrichment_separates_canonical_album_and_primary_artist_works(
        self,
        mock_search_artists,
        mock_canonical_album,
        mock_artist_details,
        mock_similar_tracks,
    ) -> None:
        mock_search_artists.return_value = [
            {"id": "tribute", "name": "Primary Artist Tribute"},
            {"id": "primary", "name": "Primary Artist"},
        ]
        mock_canonical_album.return_value = {
            "id": "canonical",
            "title": "Canonical Album",
            "artist": "Primary Artist",
        }
        mock_artist_details.return_value = {
            "top_songs": [
                {"id": "track_1", "title": "The Song", "channel": "Primary Artist"},
                {"id": "track_2", "title": "Another Song", "channel": "Primary Artist"},
            ],
            "albums": [
                {"id": "canonical", "title": "Canonical Album", "artist": "Primary Artist"},
                {"id": "related", "title": "Related Album", "artist": "Primary Artist"},
            ],
            "related_artists": [
                {"id": "similar_1", "name": "Similar Artist"},
            ],
        }
        mock_similar_tracks.return_value = [
            {"id": "similar_track", "title": "Similar Song", "channel": "Similar Artist"},
        ]

        enrichment = SearchService(server)._track_enrichment(
            "the song",
            {
                "id": "track_1",
                "title": "The Song",
                "channel": "Primary Artist",
                "album": "Canonical Album",
                "album_id": "canonical",
            },
            limit=8,
        )

        self.assertTrue(enrichment.get("applied"))
        self.assertEqual(enrichment.get("artists")[0].get("id"), "primary")
        self.assertEqual(enrichment.get("albums")[0].get("id"), "canonical")
        self.assertEqual(enrichment.get("artist_tracks")[0].get("id"), "track_2")
        self.assertEqual(enrichment.get("related_albums")[0].get("id"), "related")
        self.assertEqual(enrichment.get("similar_artists")[0].get("id"), "similar_1")
        self.assertEqual(enrichment.get("similar_tracks")[0].get("id"), "similar_track")
        self.assertEqual(
            [artist.get("id") for artist in enrichment.get("artists")],
            ["primary", "similar_1", "tribute"],
        )
        self.assertEqual(
            [album.get("id") for album in enrichment.get("albums")],
            ["canonical", "related"],
        )

    @patch("auralis_backend.search.service.search_artists_direct_cached")
    def test_low_quality_top_track_does_not_trigger_enrichment(
        self,
        mock_search_artists,
    ) -> None:
        enrichment = SearchService(server)._track_enrichment(
            "primary artist",
            {
                "id": "track_1",
                "title": "Unrelated Song",
                "channel": "Primary Artist",
                "album": "An Album",
            },
            limit=8,
        )

        self.assertFalse(enrichment.get("applied"))
        self.assertEqual(enrichment.get("artists"), [])
        self.assertEqual(enrichment.get("albums"), [])
        mock_search_artists.assert_not_called()

    @patch.object(SearchService, "_track_similar_tracks", return_value=[])
    @patch.object(server, "_build_artist_details_payload")
    @patch("auralis_backend.search.service.search_canonical_album_for_track")
    @patch("auralis_backend.search.service.search_artists_direct_cached")
    def test_track_query_resolves_topic_channel_to_canonical_artist(
        self,
        mock_search_artists,
        mock_canonical_album,
        mock_artist_details,
        _mock_similar_tracks,
    ) -> None:
        mock_search_artists.return_value = [{"id": "yes", "name": "Yes"}]
        mock_canonical_album.return_value = None
        mock_artist_details.return_value = {
            "top_songs": [],
            "albums": [],
            "related_artists": [{"id": "genesis", "name": "Genesis"}],
        }

        enrichment = SearchService(server)._track_enrichment(
            "roundabout yes",
            {
                "id": "roundabout",
                "title": "Roundabout",
                "channel": "Yes - Topic",
            },
            limit=8,
        )

        self.assertTrue(enrichment.get("applied"))
        self.assertEqual(enrichment.get("artists")[0].get("id"), "yes")
        self.assertEqual(enrichment.get("similar_artists")[0].get("id"), "genesis")

    @patch.object(SearchService, "_track_enrichment", return_value={"applied": False})
    @patch("auralis_backend.search.service.search_tracks_direct")
    def test_direct_track_response_survives_empty_enrichment(
        self,
        mock_search_tracks,
        _mock_enrichment,
    ) -> None:
        mock_search_tracks.return_value = [
            {
                "id": "track_1",
                "title": "The Song",
                "channel": "Primary Artist",
            }
        ]

        response = SearchService(server).search(
            SimpleNamespace(
                query="The Song",
                user_scope_id="guest",
                surface="search",
                force_refresh=False,
                limit=16,
                defer_side_surfaces=False,
            )
        )

        self.assertEqual(response.get("status"), "success")
        self.assertEqual(response.get("tracks")[0].get("id"), "track_1")
        self.assertEqual(response.get("artists")[0].get("name"), "Primary Artist")
        self.assertEqual(response.get("albums"), [])

    def test_deferred_search_keeps_cheap_artist_and_album_surfaces(self) -> None:
        artists, albums = SearchService(server)._cheap_track_side_surfaces(
            {
                "id": "track_1",
                "title": "The Song",
                "channel": "Primary Artist",
                "channel_id": "UC-primary",
                "album": "Primary Album",
            }
        )

        self.assertEqual(artists[0].get("id"), "UC-primary")
        self.assertEqual(albums[0].get("title"), "Primary Album")

    @patch.object(SearchService, "_track_similar_tracks")
    @patch("auralis_backend.search.service.search_canonical_album_for_track")
    @patch("auralis_backend.search.service.search_artists_direct_cached")
    @patch("auralis_backend.search.service.TRACK_ENRICHMENT_BUDGET_SECONDS", 0.03)
    def test_enrichment_returns_completed_surfaces_when_artist_surface_times_out(
        self,
        mock_search_artists,
        mock_canonical_album,
        mock_similar_tracks,
    ) -> None:
        def slow_artist_search(*_args, **_kwargs):
            time.sleep(0.15)
            return [{"id": "primary", "name": "Primary Artist"}]

        mock_search_artists.side_effect = slow_artist_search
        mock_canonical_album.return_value = {
            "id": "album_1",
            "title": "Album One",
            "artist": "Primary Artist",
        }
        mock_similar_tracks.return_value = [
            {"id": "similar_1", "title": "Similar", "channel": "Adjacent Artist"},
        ]

        started_at = time.perf_counter()
        enrichment = SearchService(server)._track_enrichment(
            "The Song",
            {
                "id": "track_1",
                "title": "The Song",
                "channel": "Primary Artist",
                "album": "Album One",
            },
            limit=12,
        )

        self.assertLess(time.perf_counter() - started_at, 0.12)
        self.assertTrue(enrichment.get("applied"))
        self.assertEqual(enrichment.get("albums")[0].get("id"), "album_1")
        self.assertEqual(enrichment.get("similar_tracks")[0].get("id"), "similar_1")
        self.assertIn("artist", enrichment.get("timed_out_surfaces"))
        self.assertIn("album", enrichment.get("completed_surfaces"))
        self.assertIn("similar_tracks", enrichment.get("completed_surfaces"))


if __name__ == "__main__":
    unittest.main()
