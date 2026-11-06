import pathlib
import sys
import unittest
from unittest.mock import Mock, patch


CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.details.detail_runtime import _artist_works, build_artist_details_payload
from auralis_backend.details.server_adapter import DetailServerAdapter


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
    def test_song_count_requires_an_exact_preview_source(self):
        server = DetailServerAdapter(_PartialFailureServer())
        server.ytmusic = Mock()
        server.ytmusic.get_song.side_effect = [
            {"videoDetails": {"videoId": "exact", "viewCount": "42"}},
            {"videoDetails": {"videoId": "alternate", "viewCount": "99"}},
        ]
        result = _artist_works(server, {
            "id": "artist", "name": "Artist",
            "top_songs": [{"videoId": "exact"}, {"videoId": "unmatched"}],
        }, "song_counts", "exact,unmatched,unrelated")
        self.assertEqual(result["items"], [{"id": "exact", "views": 42}])
        self.assertEqual([call.args[0] for call in server.ytmusic.get_song.call_args_list],
                         ["exact", "unmatched"])

    def test_optional_sections_and_deferred_write_leave_artist_playable(self):
        server = _PartialFailureServer()
        server.ytmusic = Mock()
        server.ytmusic.get_artist.return_value = {
            "name": "Primary",
            "songs": {"results": [{"videoId": "song"}]},
            "singles": {"results": [{"browseId": "single"}]},
            "playlists": {"results": [{"playlistId": "PLone", "title": "Feature"}]},
        }
        server._normalize_artist_song_entries = lambda rows, **_: [
            {"id": row["videoId"]} for row in rows
        ]
        defer = Mock()
        with patch("auralis_backend.details.detail_runtime._persist_artist_details") as persist:
            result = build_artist_details_payload(
                server, "UCprimary", lightweight=True, defer_persist=defer)
            persist.assert_not_called()
        defer.assert_called_once()
        self.assertEqual(result["top_songs"], [{"id": "song"}])
        self.assertEqual(result["singles"], [])
        self.assertEqual(result["featuring"], [])

    def test_overview_preserves_the_primary_credit_on_guest_appearances(self):
        server = _PartialFailureServer()
        server.ytmusic = Mock()
        server.ytmusic.get_artist.return_value = {
            "name": "Guest",
            "songs": {"results": [{
                "videoId": "song",
                "album": {"id": "MPRErelease", "name": "Shared release"},
                "artists": [{"id": "UCmain", "name": "Lead"}, {"id": "UCguest", "name": "Guest"}],
            }]},
        }
        server._normalize_artist_album_entries = Mock(side_effect=lambda rows, **_: rows)
        with patch("auralis_backend.details.detail_runtime._persist_artist_details"):
            result = build_artist_details_payload(server, "UCguest", lightweight=True)
        server._normalize_artist_album_entries.assert_called_with(
            [{"id": "MPRErelease", "name": "Shared release", "artist": "Lead"}],
            fallback_artist="",
        )
        self.assertEqual(result["appears_on"][0]["artist"], "Lead")

    def test_creator_header_falls_back_to_the_channel_parser(self):
        server = _PartialFailureServer()
        server.ytmusic = Mock()
        server.ytmusic.get_artist.side_effect = KeyError("musicImmersiveHeaderRenderer")
        server.ytmusic.get_user.return_value = {
            "name": "Creator",
            "videos": {"browseId": "UCcreator", "results": [{"videoId": "upload"}]},
            "playlists": {"results": [{"playlistId": "PLcreator", "title": "Playlist"}]},
        }
        server.ytmusic.get_playlist.return_value = {"tracks": [{"videoId": "curated"}]}
        server.extract_thumbnail = lambda _: None
        server._normalize_artist_song_entries = lambda rows, *, fallback_artist: [
            {"id": row["videoId"]} for row in rows
        ]
        with patch("auralis_backend.details.detail_runtime._persist_artist_details"):
            result = build_artist_details_payload(server, "UCcreator", lightweight=True)
            featured = build_artist_details_payload(
                server, "UCcreator", lightweight=True, section="featuring", collection_id="PLcreator")
            legacy = build_artist_details_payload(server, "UCcreator")
        self.assertEqual(result["name"], "Creator")
        self.assertEqual(result["top_songs"], [{"id": "upload"}])
        self.assertNotIn("songs", result["work_sections"])
        self.assertEqual(featured["items"], [{"id": "curated"}])
        self.assertEqual(legacy["name"], "Creator")
        self.assertTrue(all(call.args == ("UCcreator",) for call in server.ytmusic.get_user.call_args_list))

    def test_unrelated_provider_key_error_is_not_treated_as_creator_header(self):
        server = _PartialFailureServer()
        server.ytmusic = Mock()
        server.ytmusic.get_artist.side_effect = KeyError("unexpected")
        with self.assertRaisesRegex(KeyError, "unexpected"):
            build_artist_details_payload(server, "UCartist")
        server.ytmusic.get_user.assert_not_called()

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

    def test_web_overview_defers_full_discography_and_keeps_exact_song_credits(self):
        server = _PartialFailureServer()
        server.ytmusic = Mock()
        server.ytmusic.get_artist.return_value = {
            "name": "Credited Person",
            "albums": {"browseId": "UCperson", "params": "albums", "results": []},
            "singles": {"results": [{"browseId": "MPREsingle", "title": "Single"}]},
        }
        server.ytmusic.get_artist_albums.return_value = [
            {"browseId": f"MPREalbum{i}", "title": f"Release {i}"} for i in range(40)
        ]
        server.ytmusic.search.return_value = [
            {"videoId": "wrong", "artists": [{"id": "UCband", "name": "Credited Person"}]},
            {"videoId": "right", "artists": [{"id": "UCperson", "name": "Credited Person"}]},
        ]
        server._normalize_artist_album_entries = lambda rows, **_: [
            {"id": row["browseId"], "title": row["title"]} for row in rows
        ]
        server._normalize_artist_song_entries = lambda rows, **_: [
            {"id": row["videoId"]} for row in rows
        ]
        server.extract_thumbnail = lambda _: None
        cache = {}
        server._cache_lookup = lambda _, __, ns, key: cache.get((ns, key))
        server._cache_store = lambda _, __, ns, key, value, ttl: cache.update({(ns, key): value})
        with patch("auralis_backend.details.detail_runtime._persist_artist_details") as persist:
            artist = build_artist_details_payload(server, "MPLAUCperson", lightweight=True)
            self.assertEqual(artist["singles"][0]["id"], "MPREsingle")
            server.ytmusic.get_artist.assert_called_once_with("UCperson")
            server.ytmusic.get_artist_albums.assert_not_called()
            server.ytmusic.search.assert_not_called()
            albums = build_artist_details_payload(server, "MPLAUCperson", lightweight=True, section="albums")
            self.assertEqual(len(albums["items"]), 40)
            songs = build_artist_details_payload(server, "MPLAUCperson", lightweight=True, section="songs")
            self.assertEqual(songs["items"], [{"id": "right"}])
            build_artist_details_payload(server, "MPLAUCperson", lightweight=True)
            persist.assert_called_once()


if __name__ == "__main__":
    unittest.main()
