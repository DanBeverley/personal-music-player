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
from auralis_backend.domain.retrieval import (
    _best_track_match,
    _collect_track_candidates,
    classify_query_intent,
    load_artist_entity_expansion,
    retrieve_search_candidates_fast,
)
from auralis_backend.domain.server_adapter import adapt_domain_server
from auralis_backend.search.pipeline import rank_track_candidates_fast_path
from auralis_backend.search.pipeline import (
    rank_album_candidates_fast_path,
    rank_artist_candidates_fast_path,
)
from auralis_backend.search.intelligence import search_text_similarity
from auralis_backend.search.query_mode import resolve_search_mode
from auralis_backend.search.service import SearchService, _repair_search_artwork
from auralis_backend.search.upstream_runtime import normalize_song_result


class SearchLatencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._musicbrainz_patch = patch(
            "auralis_backend.domain.retrieval.search_musicbrainz_recording_items",
            return_value=[],
        )
        self._musicbrainz_patch.start()
        self.addCleanup(self._musicbrainz_patch.stop)

    def test_entity_names_containing_genres_stay_on_fast_path(self) -> None:
        self.assertEqual(
            resolve_search_mode(
                "Kid Rock",
                normalize_text_fn=server._normalize_text,
            ),
            "exact",
        )
        self.assertEqual(
            resolve_search_mode(
                "Iggy Pop",
                normalize_text_fn=server._normalize_text,
            ),
            "exact",
        )

    def test_search_artwork_repair_uses_matching_playable_track(self) -> None:
        tracks, albums = _repair_search_artwork(
            [
                {
                    "id": "abcdefghijk",
                    "videoId": "abcdefghijk",
                    "title": "The Trooper",
                    "artist": "Iron Maiden",
                    "album_id": "album-piece-of-mind",
                    "album": "Piece of Mind",
                }
            ],
            [
                {
                    "id": "album-piece-of-mind",
                    "title": "Piece of Mind",
                    "artist": "Iron Maiden",
                }
            ],
        )

        expected = "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg"
        self.assertEqual(tracks[0].get("thumbnail"), expected)
        self.assertEqual(albums[0].get("thumbnail"), expected)

    def test_artist_details_adapter_preserves_lightweight_contract(self) -> None:
        expected = {
            "id": "UC-Dio",
            "name": "Dio",
            "top_songs": [],
            "albums": [],
        }
        with patch.object(
            server,
            "_detail_build_artist_details_payload",
            return_value=expected,
        ) as builder:
            payload = adapt_domain_server(server).build_artist_details_payload(
                "UC-Dio",
                enrich_related=False,
                lightweight=False,
            )

        self.assertEqual(payload, expected)
        builder.assert_called_once_with(
            server,
            "UC-Dio",
            enrich_related=False,
            lightweight=False,
        )

    def test_artist_catalog_completes_top_songs_from_album_tracklists(
        self,
    ) -> None:
        artist_payload = {
            "id": "UC-Dio",
            "name": "Dio",
            "thumbnail": "dio.jpg",
            "top_songs": [
                {
                    "id": f"top-{index}",
                    "title": f"Top Song {index}",
                    "channel": "Dio",
                }
                for index in range(6)
            ],
            "albums": [
                {
                    "id": f"album-{index}",
                    "title": f"Album {index}",
                    "artist": "Dio",
                    "thumbnail": f"album-{index}.jpg",
                }
                for index in range(4)
            ],
            "related_artists": [],
        }

        def album_payload(album_id: str):
            return {
                "tracks": [
                    {
                        "id": f"{album_id}-track-{index}",
                        "title": f"{album_id} Track {index}",
                        "channel": "Dio",
                    }
                    for index in range(8)
                ]
            }

        with (
            patch.object(
                server,
                "_build_artist_details_payload",
                return_value=artist_payload,
            ),
            patch.object(
                server,
                "_assistant_tool_get_album_details",
                side_effect=album_payload,
            ),
            patch(
                "auralis_backend.domain.retrieval.catalog_playable_tracks_for_artist",
                return_value=[],
            ),
        ):
            catalog = load_artist_entity_expansion(
                server,
                artist_id="UC-Dio",
                artist_name="Dio",
                user_scope_id="artist-catalog-test",
                limit=24,
            )

        self.assertEqual(catalog.get("catalog_status"), "complete")
        self.assertGreaterEqual(len(catalog.get("tracks") or []), 20)
        self.assertGreaterEqual(
            int(catalog.get("album_tracklists_loaded") or 0),
            2,
        )
        album_track = next(
            track
            for track in catalog.get("tracks") or []
            if str(track.get("id") or "").startswith("album-0-track-")
        )
        self.assertEqual(album_track.get("thumbnail"), "album-0.jpg")

    def test_artist_catalog_replaces_unusable_track_channel_id(self) -> None:
        artist_payload = {
            "id": "UC-IronMaiden",
            "name": "Iron Maiden",
            "thumbnail": "iron-maiden.jpg",
            "top_songs": [
                {
                    "id": f"top-{index}",
                    "title": f"Top Song {index}",
                    "channel": "Iron Maiden",
                }
                for index in range(8)
            ],
            "albums": [
                {
                    "id": f"album-{index}",
                    "title": f"Album {index}",
                    "artist": "Iron Maiden",
                }
                for index in range(3)
            ],
            "related_artists": [],
        }

        def artist_details(artist_id: str, **_kwargs):
            return artist_payload if artist_id == "UC-IronMaiden" else {}

        with (
            patch.object(
                server,
                "_build_artist_details_payload",
                side_effect=artist_details,
            ),
            patch.object(
                server,
                "_assistant_tool_get_album_details",
                return_value={
                    "tracks": [
                        {
                            "id": f"album-track-{index}",
                            "title": f"Album Track {index}",
                            "channel": "Iron Maiden",
                        }
                        for index in range(16)
                    ]
                },
            ),
            patch(
                "auralis_backend.domain.retrieval.search_artists_direct_cached",
                return_value=[
                    {
                        "id": "UC-IronMaiden",
                        "name": "Iron Maiden",
                        "thumbnail": "iron-maiden.jpg",
                    }
                ],
            ),
            patch(
                "auralis_backend.domain.retrieval.catalog_playable_tracks_for_artist",
                return_value=[],
            ),
        ):
            catalog = load_artist_entity_expansion(
                server,
                artist_id="video-uploader-channel",
                artist_name="Iron Maiden",
                user_scope_id="track-channel-id-test",
                limit=24,
            )

        self.assertEqual(
            (catalog.get("artist") or {}).get("id"),
            "UC-IronMaiden",
        )
        self.assertEqual(catalog.get("catalog_status"), "complete")
        self.assertGreaterEqual(len(catalog.get("tracks") or []), 20)

    def test_ambiguous_title_prefers_authoritative_exact_artist(self) -> None:
        intent = classify_query_intent(
            server=server,
            query="Dio",
            tracks=[
                {
                    "id": "same-title",
                    "title": "Dio",
                    "channel": "Tameer Hassan",
                },
                {
                    "id": "holy-diver",
                    "title": "Holy Diver",
                    "channel": "Dio",
                },
                {
                    "id": "rainbow-dark",
                    "title": "Rainbow in the Dark",
                    "channel": "Dio",
                },
            ],
            artists=[
                {
                    "id": "UC-Dio",
                    "name": "Dio",
                    "subscribers": "1.2M",
                },
                {
                    "id": "UC-Tameer",
                    "name": "Tameer Hassan",
                    "subscribers": "900",
                },
            ],
            albums=[
                {
                    "id": "dio-holy-diver",
                    "title": "Holy Diver",
                    "artist": "Dio",
                }
            ],
        )
        self.assertEqual(intent, "artist")
        ranked = rank_track_candidates_fast_path(
            server,
            SimpleNamespace(query="Dio"),
            {
                "query_intent": intent,
                "normalized_anchor_artists": {"dio"},
                "resolved_artist": {"id": "UC-Dio", "name": "Dio"},
                "canonical_resolution": {},
                "track_candidates": {
                    "same-title": {
                        "payload": {
                            "id": "same-title",
                            "playback": {"provider": "youtube", "source_id": "00000000001"},
                            "title": "Dio",
                            "channel": "Tameer Hassan",
                        },
                        "source_scores": {"fast_query": 4.3},
                    },
                    "holy-diver": {
                        "payload": {
                            "id": "holy-diver",
                            "playback": {"provider": "youtube", "source_id": "00000000002"},
                            "title": "Holy Diver",
                            "channel": "Dio",
                        },
                        "source_scores": {"fast_query": 4.2},
                    },
                    "rainbow-dark": {
                        "payload": {
                            "id": "rainbow-dark",
                            "playback": {"provider": "youtube", "source_id": "00000000003"},
                            "title": "Rainbow in the Dark",
                            "channel": "Dio",
                        },
                        "source_scores": {"fast_query": 4.1},
                    },
                },
            },
            limit=8,
        )
        self.assertEqual(ranked[0].get("channel"), "Dio")
        self.assertNotIn(
            "Tameer Hassan",
            [item.get("channel") for item in ranked],
        )

    def test_exact_raw_artist_name_beats_punctuation_homonym(self) -> None:
        ranked = rank_artist_candidates_fast_path(
            server,
            SimpleNamespace(query="Dio"),
            {
                "query_intent": "artist",
                "resolved_artist": {"id": "UC-Dio", "name": "Dio"},
                "canonical_resolution": {},
                "artist_candidates": {
                    "dio-dollar": {
                        "payload": {
                            "id": "UC-Dio-Dollar",
                            "name": "Dio$",
                            "subscribers": "5M",
                        },
                        "source_scores": {"artists.fast": 5.0},
                    },
                    "dio": {
                        "payload": {
                            "id": "UC-Dio",
                            "name": "Dio",
                            "subscribers": "1M",
                        },
                        "source_scores": {"resolved_artist": 6.2},
                    },
                },
            },
            limit=8,
        )

        self.assertEqual(ranked[0].get("id"), "UC-Dio")

    @patch("auralis_backend.search.service.retrieve_search_candidates_fast")
    def test_selected_artist_is_shared_by_top_grid_and_artist_works(
        self,
        mock_retrieve,
    ) -> None:
        mock_retrieve.return_value = {
            "query_intent": "artist",
            "resolved_artist": {"id": "UC-Dio", "name": "Dio"},
            "normalized_anchor_artists": {"dio"},
            "track_candidates": {
                "holy-diver": {
                    "payload": {
                        "id": "holy-diver",
                        "playback": {"provider": "youtube", "source_id": "00000000001"},
                        "title": "Holy Diver",
                        "channel": "Dio",
                        "artist_id": "UC-Dio",
                    },
                    "source_scores": {"artist_catalog": 4.8},
                }
            },
            "artist_candidates": {
                "dio-dollar": {
                    "payload": {
                        "id": "UC-Dio-Dollar",
                        "name": "Dio$",
                        "subscribers": "5M",
                    },
                    "source_scores": {"artists.fast": 5.0},
                },
                "dio": {
                    "payload": {
                        "id": "UC-Dio",
                        "name": "Dio",
                        "subscribers": "1M",
                    },
                    "source_scores": {"resolved_artist": 6.2},
                },
            },
            "album_candidates": {
                "holy-diver-album": {
                    "payload": {
                        "id": "MPRE-holy-diver-album",
                        "title": "Holy Diver",
                        "artist": "Ronnie James Dio",
                    },
                    "source_scores": {"artist_discography": 4.6},
                }
            },
            "related_artists": [],
            "playlists": [],
            "retrieval_diagnostics": {},
        }
        service = SearchService(server)

        def hydrate(artists, **_kwargs):
            output = []
            for artist in artists:
                item = dict(artist)
                if item.get("id") == "UC-Dio":
                    item["thumbnail"] = "dio.jpg"
                output.append(item)
            return output

        with (
            patch.object(service, "_hydrate_artist_artwork", side_effect=hydrate),
            patch.object(service, "_lastfm_related_artists", return_value=[]),
            patch(
                "auralis_backend.search.service.catalog_playable_tracks_for_artist",
                return_value=[],
            ),
            patch(
                "auralis_backend.search.service.catalog_albums_for_artist",
                return_value=[],
            ),
            patch(
                "auralis_backend.search.service.load_artist_entity_expansion",
                return_value={
                    "artist": {"id": "UC-Dio", "name": "Dio"},
                    "tracks": [
                        mock_retrieve.return_value["track_candidates"][
                            "holy-diver"
                        ]["payload"]
                    ],
                    "albums": [
                        mock_retrieve.return_value["album_candidates"][
                            "holy-diver-album"
                        ]["payload"]
                    ],
                    "related_artists": [],
                    "catalog_status": "complete",
                },
            ),
            patch(
                "auralis_backend.search.service._SEARCH_CATALOG_WRITER.submit"
            ),
        ):
            response = service.search(
                SimpleNamespace(
                    query="Ronnie James Dio",
                    user_scope_id="canonical-artist-user",
                    surface="search",
                    force_refresh=False,
                    limit=16,
                    search_mode="entity",
                    defer_side_surfaces=False,
                    result_type="",
                    offset=0,
                )
            )

        self.assertEqual((response.get("lead_artist") or {}).get("id"), "UC-Dio")
        self.assertEqual(
            ((response.get("top_result") or {}).get("item") or {}).get("id"),
            "UC-Dio",
        )
        self.assertEqual((response.get("artists") or [])[0].get("id"), "UC-Dio")
        self.assertEqual(
            (response.get("artists") or [])[0].get("thumbnail"),
            "dio.jpg",
        )
        self.assertEqual(
            [item.get("id") for item in response.get("artist_albums") or []],
            ["MPRE-holy-diver-album"],
        )

    @patch("auralis_backend.search.service.load_artist_entity_expansion")
    @patch("auralis_backend.search.service.retrieve_search_candidates_fast")
    def test_artist_search_contract_uses_canonical_catalog_not_thin_text_batch(
        self,
        mock_retrieve,
        mock_catalog,
    ) -> None:
        mock_retrieve.return_value = {
            "query_intent": "artist",
            "resolved_artist": {
                "id": "UC-Dio",
                "name": "Dio",
                "thumbnail": "dio.jpg",
            },
            "normalized_anchor_artists": {"dio"},
            "track_candidates": {
                "holy-diver": {
                    "payload": {
                        "id": "holy-diver",
                        "playback": {"provider": "youtube", "source_id": "00000000001"},
                        "title": "Holy Diver",
                        "channel": "Dio",
                        "artist_id": "UC-Dio",
                    },
                    "source_scores": {"fast_query": 4.2},
                },
                "wrong-dio": {
                    "payload": {
                        "id": "wrong-dio",
                        "title": "Dio",
                        "channel": "Tameer Hassan",
                    },
                    "source_scores": {"fast_query": 4.3},
                },
            },
            "artist_candidates": {
                "dio": {
                    "payload": {
                        "id": "UC-Dio",
                        "name": "Dio",
                        "thumbnail": "dio.jpg",
                    },
                    "source_scores": {"resolved_artist": 6.2},
                },
                "dio-dollar": {
                    "payload": {
                        "id": "UC-Dio-Dollar",
                        "name": "Dio$",
                    },
                    "source_scores": {"fast_artist": 4.0},
                },
            },
            "album_candidates": {},
            "related_artists": [],
            "playlists": [
                {
                    "id": "dio-anthology",
                    "name": "Dio Anthology",
                    "author": "Catalog",
                    "thumbnail": "dio-playlist.jpg",
                },
                {
                    "id": "radiohead-radio",
                    "name": "Radiohead Radio",
                    "author": "Catalog",
                    "thumbnail": "radiohead.jpg",
                },
            ],
            "retrieval_diagnostics": {},
        }
        catalog_tracks = [
            {
                "id": f"dio-catalog-{index}",
                "playback": {
                    "provider": "youtube",
                    "source_id": f"{index:011d}",
                },
                "title": f"Dio Catalog Song {index}",
                "channel": "Dio",
                "artist_id": "UC-Dio",
                "thumbnail": f"track-{index}.jpg",
            }
            for index in range(24)
        ]
        catalog_albums = [
            {
                "id": f"MPRE-dio-album-{index}",
                "title": f"Dio Album {index}",
                "artist": "Dio",
                "thumbnail": f"album-{index}.jpg",
            }
            for index in range(4)
        ]
        mock_catalog.return_value = {
            "artist": {
                "id": "UC-Dio",
                "name": "Dio",
                "thumbnail": "dio.jpg",
            },
            "tracks": catalog_tracks,
            "albums": catalog_albums,
            "related_artists": [],
            "catalog_status": "complete",
            "album_tracklists_loaded": 3,
        }
        related = [
            {
                "id": f"related-{index}",
                "name": f"Related Artist {index}",
                "thumbnail": f"related-{index}.jpg",
            }
            for index in range(8)
        ]
        service = SearchService(server)
        with (
            patch.object(
                service,
                "_lastfm_related_artists",
                return_value=related,
            ),
            patch.object(
                service,
                "_resolve_first_page_related_artists",
                side_effect=lambda artists, **_kwargs: artists,
            ),
            patch.object(
                service,
                "_hydrate_artist_artwork",
                side_effect=lambda artists, **_kwargs: artists,
            ),
            patch(
                "auralis_backend.search.service._SEARCH_CATALOG_WRITER.submit"
            ),
        ):
            response = service.search(
                SimpleNamespace(
                    query="Dio",
                    user_scope_id="artist-contract-user",
                    surface="search",
                    force_refresh=True,
                    limit=16,
                    search_mode="entity",
                    defer_side_surfaces=False,
                    result_type="",
                    offset=0,
                )
            )

        self.assertEqual(
            (response.get("lead_artist") or {}).get("id"),
            "UC-Dio",
        )
        self.assertEqual(
            [artist.get("id") for artist in response.get("artists") or []],
            ["UC-Dio"],
        )
        self.assertGreaterEqual(len(response.get("artist_tracks") or []), 20)
        self.assertTrue(
            all(
                track.get("channel") == "Dio"
                for track in response.get("artist_tracks") or []
            )
        )
        self.assertNotIn(
            "wrong-dio",
            [track.get("id") for track in response.get("tracks") or []],
        )
        self.assertEqual(len(response.get("artist_albums") or []), 4)
        self.assertGreaterEqual(len(response.get("playlists") or []), 2)
        self.assertNotIn(
            "radiohead-radio",
            [
                playlist.get("id")
                for playlist in response.get("playlists") or []
            ],
        )
        self.assertEqual(len(response.get("similar_artists") or []), 8)
        self.assertTrue(
            all(
                artist.get("thumbnail")
                for artist in response.get("similar_artists") or []
            )
        )
        self.assertEqual(
            ((response.get("diagnostics") or {}).get("artist_catalog") or {}).get(
                "status"
            ),
            "complete",
        )

    @patch("auralis_backend.search.service.load_artist_entity_expansion")
    @patch("auralis_backend.search.service.retrieve_search_candidates_fast")
    def test_track_query_builds_artist_surfaces_from_credited_artist_catalog(
        self,
        mock_retrieve,
        mock_catalog,
    ) -> None:
        mock_retrieve.return_value = {
            "query_intent": "track",
            "resolved_artist": {
                "id": "UC-IronMaiden",
                "name": "Iron Maiden",
                "thumbnail": "iron-maiden.jpg",
            },
            "canonical_resolution": {
                "title": "The Trooper",
                "artist": "Iron Maiden",
                "confidence": 1.0,
            },
            "normalized_anchor_artists": {"iron maiden"},
            "track_candidates": {
                "the-trooper": {
                    "payload": {
                        "id": "the-trooper",
                        "playback": {"provider": "youtube", "source_id": "00000000001"},
                        "title": "The Trooper",
                        "channel": "Iron Maiden",
                        "artist_id": "UC-IronMaiden",
                    },
                    "source_scores": {"fast_query": 4.3},
                },
                "unrelated-provider-result": {
                    "payload": {
                        "id": "unrelated-provider-result",
                        "title": "All My Boast Is In Jesus",
                        "channel": "The Worship Initiative",
                    },
                    "source_scores": {"provider_intent": 4.3},
                },
            },
            "artist_candidates": {
                "iron-maiden": {
                    "payload": {
                        "id": "UC-IronMaiden",
                        "name": "Iron Maiden",
                        "thumbnail": "iron-maiden.jpg",
                    },
                    "source_scores": {"credited_artist": 5.8},
                },
            },
            "album_candidates": {
                "piece-of-mind": {
                    "payload": {
                        "id": "MPRE-piece-of-mind",
                        "title": "Piece of Mind",
                        "artist": "Iron Maiden",
                    },
                    "source_scores": {"track_album": 5.3},
                },
            },
            "related_artists": [],
            "playlists": [],
            "retrieval_diagnostics": {},
        }
        catalog_tracks = [
            {
                "id": f"iron-maiden-track-{index}",
                "playback": {
                    "provider": "youtube",
                    "source_id": f"{index:011d}",
                },
                "title": f"Iron Maiden Catalog Track {index}",
                "channel": "Iron Maiden",
                "artist_id": "UC-IronMaiden",
            }
            for index in range(24)
        ]
        catalog_albums = [
            {
                "id": f"MPRE-iron-maiden-album-{index}",
                "title": f"Iron Maiden Album {index}",
                "artist": "Iron Maiden",
            }
            for index in range(6)
        ]
        mock_catalog.return_value = {
            "artist": {
                "id": "UC-IronMaiden",
                "name": "Iron Maiden",
                "thumbnail": "iron-maiden.jpg",
            },
            "tracks": catalog_tracks,
            "albums": catalog_albums,
            "related_artists": [],
            "catalog_status": "complete",
            "album_tracklists_loaded": 3,
        }
        related = [
            {
                "id": f"related-{index}",
                "name": f"Related Metal Artist {index}",
                "thumbnail": f"related-{index}.jpg",
            }
            for index in range(8)
        ]
        service = SearchService(server)
        with (
            patch.object(
                service,
                "_lastfm_related_artists",
                return_value=related,
            ),
            patch.object(
                service,
                "_hydrate_artist_artwork",
                side_effect=lambda artists, **_kwargs: artists,
            ),
            patch(
                "auralis_backend.search.service._SEARCH_CATALOG_WRITER.submit"
            ),
        ):
            response = service.search(
                SimpleNamespace(
                    query="The Trooper",
                    user_scope_id="track-artist-contract-user",
                    surface="search",
                    force_refresh=True,
                    limit=16,
                    search_mode="entity",
                    defer_side_surfaces=False,
                    result_type="",
                    offset=0,
                )
            )

        self.assertEqual(
            (response.get("lead_artist") or {}).get("id"),
            "UC-IronMaiden",
        )
        self.assertGreaterEqual(len(response.get("artist_tracks") or []), 20)
        self.assertGreaterEqual(len(response.get("artist_albums") or []), 6)
        self.assertNotIn(
            "unrelated-provider-result",
            [track.get("id") for track in response.get("tracks") or []],
        )
        self.assertTrue(
            all(
                track.get("channel") == "Iron Maiden"
                for track in response.get("artist_tracks") or []
            )
        )
        self.assertEqual(len(response.get("similar_artists") or []), 8)
        self.assertTrue(
            all(
                artist.get("thumbnail")
                for artist in response.get("similar_artists") or []
            )
        )

    def test_indirect_and_multi_signal_queries_use_exploration_path(self) -> None:
        self.assertEqual(
            resolve_search_mode(
                "that song about cavalry by iron maiden",
                normalize_text_fn=server._normalize_text,
            ),
            "taste",
        )
        self.assertEqual(
            resolve_search_mode(
                "80s heavy metal workout",
                normalize_text_fn=server._normalize_text,
            ),
            "taste",
        )

    def test_artist_typo_resolves_as_artist_after_mixed_retrieval(self) -> None:
        intent = classify_query_intent(
            server=adapt_domain_server(server),
            query="iron maden",
            tracks=[
                {
                    "id": f"track-{index}",
                    "title": title,
                    "channel": "Iron Maiden",
                }
                for index, title in enumerate(
                    ["The Trooper", "Hallowed Be Thy Name", "Run to the Hills"]
                )
            ],
            artists=[{"id": "artist-1", "name": "Iron Maiden"}],
            albums=[],
        )
        self.assertEqual(intent, "artist")

    def test_supported_artist_beats_same_name_album(self) -> None:
        intent = classify_query_intent(
            server=adapt_domain_server(server),
            query="Nirvana",
            tracks=[
                {
                    "id": f"nirvana-track-{index}",
                    "title": title,
                    "channel": "Nirvana",
                    "artist_id": "UC-main-nirvana",
                }
                for index, title in enumerate(
                    ["Smells Like Teen Spirit", "Come As You Are", "Lithium"]
                )
            ],
            artists=[
                {
                    "id": "UC-main-nirvana",
                    "name": "Nirvana",
                    "subscribers": "10M",
                },
                {
                    "id": "UC-obscure-nirvana",
                    "name": "Nirvana",
                    "subscribers": "17",
                },
            ],
            albums=[
                {
                    "id": "same-name-album",
                    "title": "Nirvana",
                    "artist": "Other Artist",
                }
            ],
        )
        self.assertEqual(intent, "artist")

    def test_exact_track_beats_same_name_artist_without_catalog_support(self) -> None:
        intent = classify_query_intent(
            server=adapt_domain_server(server),
            query="The Trooper",
            tracks=[
                {
                    "id": "iron-maiden-trooper",
                    "title": "The Trooper",
                    "channel": "Iron Maiden",
                },
                {
                    "id": "cover-trooper",
                    "title": "The Trooper",
                    "channel": "Other Artist",
                },
            ],
            artists=[{"id": "obscure-artist", "name": "The Trooper"}],
            albums=[],
        )
        self.assertEqual(intent, "track")

    def test_best_track_match_accepts_compact_provider_popularity(self) -> None:
        result = _best_track_match(
            adapt_domain_server(server),
            "crazy little thing called love",
            [
                {
                    "id": "queen",
                    "title": "Crazy Little Thing Called Love",
                    "channel": "Queen",
                    "views": "301M",
                },
                {
                    "id": "cover",
                    "title": "Crazy Little Thing Called Love",
                    "channel": "Cover Artist",
                    "views": "845K",
                },
            ],
        )
        self.assertEqual(result["id"], "queen")

    def test_partial_and_reordered_text_match_canonical_title(self) -> None:
        self.assertGreaterEqual(
            search_text_similarity("water smoke", "Smoke on the Water"),
            0.75,
        )
        self.assertGreaterEqual(
            search_text_similarity("bohemain rapsody", "Bohemian Rhapsody"),
            0.75,
        )

    def test_exact_track_and_album_intents_remain_distinct(self) -> None:
        domain_server = adapt_domain_server(server)
        track_intent = classify_query_intent(
            server=domain_server,
            query="Fear of the Dark",
            tracks=[
                {
                    "id": "track-1",
                    "title": "Fear of the Dark",
                    "channel": "Iron Maiden",
                }
            ],
            artists=[{"id": "artist-1", "name": "Iron Maiden"}],
            albums=[],
        )
        album_intent = classify_query_intent(
            server=domain_server,
            query="Powerslave",
            tracks=[
                {
                    "id": "track-2",
                    "title": "Aces High",
                    "channel": "Iron Maiden",
                }
            ],
            artists=[{"id": "artist-1", "name": "Iron Maiden"}],
            albums=[
                {
                    "id": "album-1",
                    "title": "Powerslave",
                    "artist": "Iron Maiden",
                }
            ],
        )
        self.assertEqual(track_intent, "track")
        self.assertEqual(album_intent, "album")

    def test_track_candidates_merge_by_canonical_song_identity(self) -> None:
        candidates = {}
        domain_server = adapt_domain_server(server)
        _collect_track_candidates(
            candidates,
            server=domain_server,
            tracks=[
                {
                    "id": "cached_low",
                    "title": "Highway to Hell",
                    "channel": "AC/DC",
                    "thumbnail": "",
                }
            ],
            source_name="catalog_memory",
            base_score=2.0,
        )
        _collect_track_candidates(
            candidates,
            server=domain_server,
            tracks=[
                {
                    "id": "live_better",
                    "title": "Highway to Hell",
                    "channel": "AC/DC",
                    "album": "Highway to Hell",
                    "thumbnail": "cover.jpg",
                }
            ],
            source_name="ytmusic_live",
            base_score=4.8,
        )

        self.assertEqual(len(candidates), 1)
        candidate = next(iter(candidates.values()))
        self.assertEqual(candidate["payload"].get("id"), "live_better")
        self.assertEqual(candidate["payload"].get("album"), "Highway to Hell")
        self.assertEqual(
            set(candidate.get("source_scores") or {}),
            {"catalog_memory", "ytmusic_live"},
        )

    def test_artist_query_is_not_capped_at_two_tracks(self) -> None:
        retrieval_payload = {
            "query_intent": "artist",
            "normalized_anchor_artists": {"iron maiden"},
            "track_candidates": {
                f"track-{index}": {
                    "payload": {
                        "id": f"track-{index}",
                        "playback": {"provider": "youtube", "source_id": f"{index:011d}"},
                        "title": title,
                        "channel": "Iron Maiden",
                    },
                    "source_scores": {"fast_query": 4.0 - index * 0.1},
                }
                for index, title in enumerate(
                    [
                        "The Trooper",
                        "Run to the Hills",
                        "Fear of the Dark",
                        "Aces High",
                        "Wasted Years",
                    ]
                )
            },
        }
        results = rank_track_candidates_fast_path(
            server,
            SimpleNamespace(query="Iron Maiden"),
            retrieval_payload,
            limit=5,
        )
        self.assertGreater(len(results), 2)

    def test_provider_track_keeps_credited_artist_identity(self) -> None:
        normalized = normalize_song_result(
            server,
            {
                "videoId": "000trooper0",
                "title": "The Trooper",
                "artists": [
                    {
                        "name": "Iron Maiden",
                        "id": "UC-IronMaiden",
                    }
                ],
                "album": {
                    "name": "Piece of Mind",
                    "id": "MPRE-piece-of-mind",
                },
            },
        )
        self.assertEqual(normalized.get("artist_id"), "UC-IronMaiden")
        self.assertEqual(
            (normalized.get("artist_entities") or [])[0].get("name"),
            "Iron Maiden",
        )

    def test_track_query_cross_tabs_use_credited_artist_and_album(self) -> None:
        retrieval_payload = {
            "query_intent": "track",
            "normalized_anchor_artists": {"iron maiden"},
            "track_candidates": {},
            "artist_candidates": {
                "UC-IronMaiden": {
                    "payload": {
                        "id": "UC-IronMaiden",
                        "name": "Iron Maiden",
                    },
                    "source_scores": {"credited_artist": 5.8},
                }
            },
            "album_candidates": {
                "piece-of-mind": {
                    "payload": {
                        "id": "piece-of-mind",
                        "title": "Piece of Mind",
                        "artist": "Iron Maiden",
                    },
                    "source_scores": {"track_album": 5.3},
                }
            },
        }
        artists = rank_artist_candidates_fast_path(
            server,
            SimpleNamespace(query="The Trooper"),
            retrieval_payload,
            limit=8,
        )
        albums = rank_album_candidates_fast_path(
            server,
            SimpleNamespace(query="The Trooper"),
            retrieval_payload,
            limit=8,
        )
        self.assertEqual(artists[0].get("name"), "Iron Maiden")
        self.assertEqual(albums[0].get("title"), "Piece of Mind")

    def test_track_query_lead_cannot_drift_from_resolved_artist(self) -> None:
        lead_artist, containing_album = SearchService(server)._lead_entities(
            query="The Trooper",
            query_intent="track",
            tracks=[
                {
                    "id": "stale-queen-result",
                    "title": "Another One Bites the Dust",
                    "channel": "Queen",
                    "artist_id": "UC-Queen",
                    "album": "The Game",
                },
                {
                    "id": "trooper",
                    "title": "The Trooper",
                    "channel": "Iron Maiden",
                    "artist_id": "UC-IronMaiden",
                    "album": "Piece of Mind",
                },
            ],
            artists=[
                {"id": "UC-Queen", "name": "Queen"},
                {"id": "UC-IronMaiden", "name": "Iron Maiden"},
            ],
            albums=[
                {"id": "the-game", "title": "The Game", "artist": "Queen"},
                {
                    "id": "piece-of-mind",
                    "title": "Piece of Mind",
                    "artist": "Iron Maiden",
                },
            ],
            resolved_artist={
                "id": "UC-IronMaiden",
                "name": "Iron Maiden",
            },
        )

        self.assertEqual(lead_artist.get("name"), "Iron Maiden")
        self.assertEqual(containing_album.get("title"), "Piece of Mind")

    def test_track_query_admits_same_artist_catalog_below_exact_match(self) -> None:
        candidates = {
            "trooper": {
                "payload": {
                    "id": "trooper",
                    "playback": {"provider": "youtube", "source_id": "00000000001"},
                    "title": "The Trooper",
                    "channel": "Iron Maiden",
                },
                "source_scores": {"fast_query": 4.3},
            }
        }
        for index, title in enumerate(
            [
                "Run to the Hills",
                "Fear of the Dark",
                "Aces High",
                "Wasted Years",
                "Hallowed Be Thy Name",
                "Powerslave",
            ]
        ):
            candidates[f"catalog-{index}"] = {
                "payload": {
                    "id": f"catalog-{index}",
                    "playback": {
                        "provider": "youtube",
                        "source_id": f"{index + 2:011d}",
                    },
                    "title": title,
                    "channel": "Iron Maiden",
                },
                "source_scores": {"same_artist_catalog": 4.2 - index * 0.05},
            }
        results = rank_track_candidates_fast_path(
            server,
            SimpleNamespace(query="The Trooper"),
            {
                "query_intent": "track",
                "normalized_anchor_artists": {"iron maiden"},
                "track_candidates": candidates,
            },
            limit=12,
        )
        self.assertEqual(results[0].get("id"), "trooper")
        self.assertGreaterEqual(len(results), 7)

    def test_canonical_artist_beats_provider_first_cover_for_typo(self) -> None:
        results = rank_track_candidates_fast_path(
            server,
            SimpleNamespace(query="bohemian rhapsodu"),
            {
                "query_intent": "track",
                "normalized_anchor_artists": set(),
                "canonical_resolution": {
                    "title": "Bohemian Rhapsody",
                    "artist": "Queen",
                    "confidence": 0.98,
                },
                "track_candidates": {
                    "cover": {
                        "payload": {
                            "id": "cover",
                            "playback": {"provider": "youtube", "source_id": "00000000001"},
                            "title": "Bohemian Rhapsody",
                            "channel": "Panic! At The Disco",
                        },
                        "source_scores": {"fast_query": 4.3},
                    },
                    "original": {
                        "payload": {
                            "id": "original",
                            "playback": {"provider": "youtube", "source_id": "00000000002"},
                            "title": "Bohemian Rhapsody",
                            "channel": "Queen",
                        },
                        "source_scores": {"fast_query": 4.3},
                    },
                },
            },
            limit=8,
        )
        self.assertEqual(results[0].get("id"), "original")
        self.assertGreater(
            results[0]["ranking_features"]["canonical_pair_match"],
            results[1]["ranking_features"]["canonical_pair_match"],
        )

    def test_indirect_query_trusts_provider_intent_without_history_fillers(self) -> None:
        retrieval_payload = {
            "query_intent": "mixed",
            "normalized_anchor_artists": {"iron maiden"},
            "track_candidates": {
                "trooper": {
                    "payload": {
                        "id": "trooper",
                        "playback": {"provider": "youtube", "source_id": "00000000001"},
                        "title": "The Trooper",
                        "channel": "Iron Maiden",
                    },
                    "source_scores": {"provider_intent": 4.3},
                },
                "history": {
                    "payload": {
                        "id": "history",
                        "playback": {"provider": "youtube", "source_id": "00000000002"},
                        "title": "Another One Bites the Dust",
                        "channel": "Queen",
                    },
                    "source_scores": {"catalog_fuzzy": 20.0},
                },
            },
        }
        results = rank_track_candidates_fast_path(
            server,
            SimpleNamespace(query="that cavalry song by iron maiden"),
            retrieval_payload,
            limit=8,
        )
        self.assertEqual([item.get("id") for item in results], ["trooper"])

    @patch("auralis_backend.domain.retrieval._retrieval_cache_set")
    @patch("auralis_backend.domain.retrieval._retrieval_cache_get", return_value=None)
    @patch(
        "auralis_backend.domain.retrieval.load_fuzzy_catalog_entity_memories",
        return_value=[],
    )
    @patch("auralis_backend.domain.retrieval.search_albums_direct", return_value=[])
    @patch("auralis_backend.domain.retrieval.search_artists_direct_cached")
    @patch("auralis_backend.domain.retrieval.search_tracks_direct")
    def test_artist_retrieval_leaves_structured_catalog_to_search_service(
        self,
        mock_track_search,
        mock_artist_search,
        _mock_album_search,
        _mock_fuzzy,
        _mock_cache_get,
        _mock_cache_set,
    ) -> None:
        mock_track_search.return_value = [
            {
                "id": "trooper",
                "playback": {"provider": "youtube", "source_id": "00000000001"},
                "title": "The Trooper",
                "channel": "Iron Maiden",
                "artist_id": "UC-IronMaiden",
            },
            {
                "id": "fear",
                "playback": {"provider": "youtube", "source_id": "00000000002"},
                "title": "Fear of the Dark",
                "channel": "Iron Maiden",
                "artist_id": "UC-IronMaiden",
            },
            {
                "id": "aces",
                "playback": {"provider": "youtube", "source_id": "00000000003"},
                "title": "Aces High",
                "channel": "Iron Maiden",
                "artist_id": "UC-IronMaiden",
            },
        ]
        mock_artist_search.return_value = [
            {
                "id": "UC-IronMaiden",
                "name": "Iron Maiden",
                "subscribers": "3M",
            }
        ]
        payload = retrieve_search_candidates_fast(
            SimpleNamespace(
                query="Iron Maiden",
                surface="search",
                force_refresh=False,
                search_mode="exact",
                anchor_track_snapshots=[],
            ),
            {
                "user_scope_id": "guest",
                "recent_queries": [],
                "last_played_tracks": [],
            },
            limit=24,
        )
        tracks = rank_track_candidates_fast_path(
            server,
            SimpleNamespace(query="Iron Maiden"),
            payload,
            limit=24,
        )
        albums = rank_album_candidates_fast_path(
            server,
            SimpleNamespace(query="Iron Maiden"),
            payload,
            limit=12,
        )
        self.assertEqual(payload.get("query_intent"), "artist")
        self.assertEqual(len(tracks), 3)
        self.assertEqual(albums, [])
        self.assertEqual(payload.get("related_artists") or [], [])

    @patch("auralis_backend.domain.retrieval._retrieval_cache_set")
    @patch("auralis_backend.domain.retrieval._retrieval_cache_get", return_value=None)
    @patch(
        "auralis_backend.domain.retrieval.load_fuzzy_catalog_entity_memories",
        return_value=[],
    )
    @patch("auralis_backend.domain.retrieval.search_albums_direct", return_value=[])
    @patch("auralis_backend.domain.retrieval.search_artists_direct_cached")
    @patch("auralis_backend.domain.retrieval.search_tracks_direct")
    def test_typo_artist_resolution_uses_dominant_credited_artist(
        self,
        mock_track_search,
        mock_artist_search,
        _mock_album_search,
        _mock_fuzzy,
        _mock_cache_get,
        _mock_cache_set,
    ) -> None:
        mock_track_search.return_value = [
            {
                "id": f"arctic-{index}",
                "title": f"Arctic Song {index}",
                "channel": "Arctic Monkeys",
                "artist_id": "UC-ArcticMonkeys",
            }
            for index in range(8)
        ]
        mock_artist_search.return_value = [
            {
                "id": "UC-WrongSingular",
                "name": "Arctic Monkey",
                "subscribers": "20",
            },
            {
                "id": "UC-ArcticMonkeys",
                "name": "Arctic Monkeys",
                "subscribers": "9.5M",
            },
        ]
        payload = retrieve_search_candidates_fast(
            SimpleNamespace(
                query="artic monkey",
                surface="search",
                force_refresh=False,
                search_mode="entity",
                anchor_track_snapshots=[],
            ),
            {
                "user_scope_id": "guest",
                "recent_queries": [],
                "last_played_tracks": [],
            },
            limit=24,
        )

        self.assertEqual(payload.get("query_intent"), "artist")
        self.assertEqual(
            (payload.get("resolved_artist") or {}).get("id"),
            "UC-ArcticMonkeys",
        )

    def test_exact_track_search_rejects_unrelated_history_fillers(self) -> None:
        retrieval_payload = {
            "query_intent": "track",
            "normalized_anchor_artists": {"iron maiden"},
            "track_candidates": {
                "trooper": {
                    "payload": {
                        "id": "trooper",
                        "playback": {"provider": "youtube", "source_id": "00000000001"},
                        "title": "The Trooper",
                        "channel": "Iron Maiden",
                    },
                    "source_scores": {"fast_query": 4.0},
                },
                "history": {
                    "payload": {
                        "id": "history",
                        "playback": {"provider": "youtube", "source_id": "00000000002"},
                        "title": "Another One Bites the Dust",
                        "channel": "Queen",
                    },
                    "source_scores": {"catalog_memory": 20.0},
                },
            },
        }
        results = rank_track_candidates_fast_path(
            server,
            SimpleNamespace(query="The Trooper"),
            retrieval_payload,
            limit=8,
        )
        self.assertEqual([item.get("id") for item in results], ["trooper"])

    def test_musicbrainz_identity_without_playback_is_not_returned_as_track(self) -> None:
        retrieval_payload = {
            "query_intent": "track",
            "normalized_anchor_artists": {"iron maiden"},
            "track_candidates": {
                "musicbrainz": {
                    "payload": {
                        "id": "musicbrainz:recording:trooper",
                        "title": "The Trooper",
                        "artist": "Iron Maiden",
                        "source_provider": "musicbrainz",
                        "playable": False,
                    },
                    "source_scores": {"catalog_fuzzy": 9.0},
                }
            },
        }
        results = rank_track_candidates_fast_path(
            server,
            SimpleNamespace(query="The Trooper"),
            retrieval_payload,
            limit=8,
        )
        self.assertEqual(results, [])

    @patch("auralis_backend.domain.retrieval._retrieval_cache_set")
    @patch("auralis_backend.domain.retrieval._retrieval_cache_get", return_value=None)
    @patch("auralis_backend.domain.retrieval.load_fuzzy_catalog_entity_memories")
    @patch("auralis_backend.domain.retrieval.search_albums_direct", return_value=[])
    @patch("auralis_backend.domain.retrieval.search_artists_direct_cached", return_value=[])
    @patch(
        "auralis_backend.domain.retrieval.catalog_playable_tracks_for_artist",
        return_value=[],
    )
    @patch("auralis_backend.domain.retrieval.search_tracks_direct")
    def test_canonical_identity_launches_exact_playable_resolution(
        self,
        mock_track_search,
        _mock_artist_catalog,
        _mock_artist_search,
        _mock_album_search,
        mock_fuzzy,
        _mock_cache_get,
        _mock_cache_set,
    ) -> None:
        mock_fuzzy.return_value = [
            {
                "entity_type": "track",
                "confidence": 0.96,
                "payload": {
                    "id": "musicbrainz:recording:trooper",
                    "title": "The Trooper",
                    "artist": "Iron Maiden",
                    "source_provider": "musicbrainz",
                    "playable": False,
                },
            }
        ]

        def track_results(query, *_args, **_kwargs):
            if query == "The Trooper Iron Maiden":
                return [
                    {
                        "id": "youtube-trooper",
                        "playback": {"provider": "youtube", "source_id": "00000000001"},
                        "title": "The Trooper",
                        "channel": "Iron Maiden",
                        "source_provider": "ytmusic",
                    }
                ]
            return [
                {
                    "id": "wrong",
                    "playback": {"provider": "youtube", "source_id": "00000000002"},
                    "title": "The Trooper",
                    "channel": "The HU",
                }
            ]

        mock_track_search.side_effect = track_results
        payload = retrieve_search_candidates_fast(
            SimpleNamespace(
                query="The Trooper",
                surface="search",
                force_refresh=False,
                search_mode="exact",
                anchor_track_snapshots=[],
            ),
            {
                "user_scope_id": "guest",
                "recent_queries": [],
                "last_played_tracks": [],
            },
            limit=8,
        )
        results = rank_track_candidates_fast_path(
            server,
            SimpleNamespace(query="The Trooper"),
            payload,
            limit=8,
        )
        self.assertEqual(results[0].get("id"), "youtube-trooper")
        self.assertEqual(
            (payload.get("retrieval_diagnostics") or {}).get(
                "canonical_track_query"
            ),
            "The Trooper Iron Maiden",
        )

    @patch("auralis_backend.domain.retrieval._retrieval_cache_set")
    @patch("auralis_backend.domain.retrieval._retrieval_cache_get", return_value=None)
    @patch("auralis_backend.domain.retrieval.search_albums_direct")
    @patch("auralis_backend.domain.retrieval.search_artists_direct_cached")
    @patch("auralis_backend.domain.retrieval.search_tracks_direct")
    def test_fast_retrieval_uses_direct_search_helpers(
        self,
        mock_tracks_direct,
        mock_artists_direct,
        mock_albums_direct,
        _mock_cache_get,
        _mock_cache_set,
    ) -> None:
        mock_tracks_direct.return_value = [
            {
                "id": "track_1",
                "title": "Smells Like Teen Spirit",
                "channel": "Nirvana",
                "album": "Nevermind",
            }
        ]
        mock_artists_direct.return_value = [
            {
                "id": "artist_1",
                "name": "Nirvana",
            }
        ]
        mock_albums_direct.return_value = [
            {
                "id": "album_1",
                "title": "Nevermind",
                "artist": "Nirvana",
            }
        ]
        payload = retrieve_search_candidates_fast(
            SimpleNamespace(
                query="smell like teen spirit",
                surface="search",
                force_refresh=False,
                anchor_track_snapshots=[],
                recent_track_snapshots=[],
                last_played_tracks=[],
                recent_queries=[],
                taste_queries=[],
                artist_hints=[],
            ),
            {
                "user_scope_id": "guest",
                "recent_queries": [],
                "last_played_tracks": [],
            },
            limit=8,
        )
        self.assertEqual(payload.get("query_intent"), "track")
        self.assertEqual(len(dict(payload.get("track_candidates") or {})), 1)
        self.assertEqual(len(dict(payload.get("artist_candidates") or {})), 1)
        self.assertEqual(len(dict(payload.get("album_candidates") or {})), 1)
        diagnostics = dict(payload.get("retrieval_diagnostics") or {})
        self.assertIn("tracks.fast", list(diagnostics.get("completed_sources") or []))
        self.assertIn("artists.fast", list(diagnostics.get("completed_sources") or []))
        self.assertIn("albums.fast", list(diagnostics.get("completed_sources") or []))
        self.assertNotIn(
            "playlists.fast",
            list(diagnostics.get("completed_sources") or []),
        )
        self.assertEqual(
            (diagnostics.get("provider_plan") or ""),
            "typed_parallel",
        )
        self.assertEqual(mock_artists_direct.call_count, 1)
        self.assertEqual(
            mock_artists_direct.call_args_list[0].args[:2],
            ("smell like teen spirit", 12),
        )
        mock_albums_direct.assert_called_once()

    def test_strong_local_artist_memory_skips_redundant_typed_provider_calls(self) -> None:
        with (
            patch(
                "auralis_backend.domain.retrieval._retrieval_cache_get",
                return_value=None,
            ),
            patch(
                "auralis_backend.domain.retrieval.load_fuzzy_catalog_entity_memories",
                return_value=[
                    {
                        "entity_type": "artist",
                        "payload": {
                            "id": "UC-LocalArtist",
                            "name": "Local Artist",
                            "thumbnail": "local.jpg",
                        },
                    }
                ],
            ),
            patch(
                "auralis_backend.domain.retrieval.search_tracks_direct",
                return_value=[
                    {
                        "id": "live-track",
                        "title": "Local Artist Song",
                        "channel": "Local Artist",
                    }
                ],
            ) as live_tracks,
            patch(
                "auralis_backend.domain.retrieval.search_artists_direct_cached",
                return_value=[
                    {
                        "id": "UC-LocalArtist",
                        "name": "Local Artist",
                    }
                ],
            ) as live_artists,
            patch(
                "auralis_backend.domain.retrieval.search_albums_direct",
                return_value=[
                    {
                        "id": "live-album",
                        "title": "Local Album",
                        "artist": "Local Artist",
                    }
                ],
            ) as live_albums,
        ):
            payload = retrieve_search_candidates_fast(
                SimpleNamespace(
                    query="Local Artist",
                    surface="search",
                    force_refresh=False,
                    anchor_track_snapshots=[],
                    recent_track_snapshots=[],
                    last_played_tracks=[],
                    recent_queries=[],
                    taste_queries=[],
                    artist_hints=[],
                ),
                {
                    "user_scope_id": "local-index-user",
                    "recent_queries": [],
                    "last_played_tracks": [],
                },
                limit=16,
            )

        self.assertEqual(
            (payload.get("retrieval_diagnostics") or {}).get("mode"),
            "fast_query_fallback",
        )
        live_tracks.assert_called_once()
        live_artists.assert_not_called()
        live_albums.assert_not_called()

    @patch("auralis_backend.search.service.retrieve_search_candidates_fast")
    def test_two_word_exact_query_uses_direct_path_without_profile_build(
        self,
        mock_retrieve,
    ) -> None:
        mock_retrieve.return_value = {
            "query_intent": "track",
            "normalized_anchor_artists": {"prince"},
            "track_candidates": {
                "purple-rain": {
                    "payload": {
                        "id": "purple-rain",
                        "playback": {"provider": "youtube", "source_id": "00000000001"},
                        "title": "Purple Rain",
                        "channel": "Prince",
                        "album": "Purple Rain",
                    },
                    "source_scores": {"fast_query": 4.3},
                }
            },
            "artist_candidates": {},
            "album_candidates": {},
            "retrieval_diagnostics": {},
        }
        response = SearchService(server).search(
            SimpleNamespace(
                query="Purple Rain",
                user_scope_id="guest",
                surface="search",
                force_refresh=False,
                limit=16,
                search_mode="exact",
                defer_side_surfaces=True,
            )
        )
        self.assertEqual(response.get("status"), "success")
        self.assertEqual((response.get("tracks") or [])[0].get("id"), "purple-rain")
        self.assertTrue(bool((response.get("diagnostics") or {}).get("direct_search_only")))
        self.assertTrue(bool((response.get("diagnostics") or {}).get("profile_build_skipped")))

    @patch("auralis_backend.search.service.retrieve_search_candidates_fast")
    def test_complete_first_page_reuses_one_retrieval_for_all_surfaces(
        self,
        mock_retrieve,
    ) -> None:
        mock_retrieve.return_value = {
            "query_intent": "artist",
            "normalized_anchor_artists": {"queen"},
            "track_candidates": {
                f"queen-track-{index}": {
                    "payload": {
                        "id": f"queen-track-{index}",
                        "playback": {"provider": "youtube", "source_id": f"{index:011d}"},
                        "title": f"Queen Song {index}",
                        "channel": "Queen",
                    },
                    "source_scores": {"artist_catalog": 4.8},
                }
                for index in range(8)
            },
            "artist_candidates": {
                "queen": {
                    "payload": {
                        "id": "UC-Queen",
                        "name": "Queen",
                        "thumbnail": "queen.jpg",
                    },
                    "source_scores": {"resolved_artist": 6.2},
                }
            },
            "album_candidates": {
                f"queen-album-{index}": {
                    "payload": {
                        "id": f"MPRE-queen-album-{index}",
                        "title": f"Queen Album {index}",
                        "artist": "Queen",
                    },
                    "source_scores": {"artist_discography": 4.6},
                }
                for index in range(4)
            },
            "playlists": [
                {
                    "id": "queen-playlist",
                    "name": "Queen Essentials",
                    "thumbnail": "playlist.jpg",
                }
            ],
            "related_artists": [],
            "retrieval_diagnostics": {"provider_plan": "typed_parallel"},
        }
        service = SearchService(server)
        with (
            patch.object(
                service,
                "_lastfm_related_artists",
                return_value=[
                    {
                        "id": "UC-DavidBowie",
                        "name": "David Bowie",
                        "thumbnail": "bowie.jpg",
                    },
                    {
                        "id": "UC-MilesKane-1",
                        "name": "Miles Kane",
                        "thumbnail": "miles-one.jpg",
                    },
                    {
                        "id": "UC-MilesKane-2",
                        "name": "Miles Kane",
                        "thumbnail": "miles-two.jpg",
                    },
                ],
            ),
            patch(
                "auralis_backend.search.service.catalog_playable_tracks_for_artist",
                return_value=[],
            ),
            patch(
                "auralis_backend.search.service.catalog_albums_for_artist",
                return_value=[],
            ),
            patch(
                "auralis_backend.search.service.load_artist_entity_expansion",
                return_value={
                    "artist": {
                        "id": "UC-Queen",
                        "name": "Queen",
                        "thumbnail": "queen.jpg",
                    },
                    "tracks": [
                        entry["payload"]
                        for entry in mock_retrieve.return_value[
                            "track_candidates"
                        ].values()
                    ],
                    "albums": [
                        entry["payload"]
                        for entry in mock_retrieve.return_value[
                            "album_candidates"
                        ].values()
                    ],
                    "related_artists": [],
                    "catalog_status": "complete",
                },
            ),
        ):
            response = service.search(
                SimpleNamespace(
                    query="Queen",
                    user_scope_id="complete-search-user",
                    surface="search",
                    force_refresh=False,
                    limit=16,
                    search_mode="entity",
                    defer_side_surfaces=False,
                    result_type="",
                    offset=0,
                )
            )

        self.assertEqual(mock_retrieve.call_count, 1)
        self.assertEqual(len(response.get("artist_tracks") or []), 8)
        self.assertEqual(len(response.get("artist_albums") or []), 4)
        self.assertEqual(
            [item.get("name") for item in response.get("similar_artists") or []],
            ["David Bowie", "Miles Kane"],
        )
        playlist_ids = [
            item.get("id") for item in response.get("playlists") or []
        ]
        self.assertEqual(playlist_ids[0], "queen-playlist")
        self.assertTrue(
            any(
                str(playlist_id).startswith("search-generated:uc-queen:")
                for playlist_id in playlist_ids
            )
        )

    @patch("auralis_backend.search.service.retrieve_search_candidates_fast")
    def test_load_more_reads_ranked_search_snapshot_without_retrieval(
        self,
        mock_retrieve,
    ) -> None:
        query = "snapshot anthem"
        mock_retrieve.return_value = {
            "query_intent": "track",
            "normalized_anchor_artists": {"test artist"},
            "track_candidates": {
                f"track-{index}": {
                    "payload": {
                        "id": f"track-{index}",
                        "playback": {"provider": "youtube", "source_id": f"{index:011d}"},
                        "title": f"Snapshot Anthem {index}",
                        "channel": "Test Artist",
                    },
                    "source_scores": {"fast_query": 4.3 - index * 0.01},
                }
                for index in range(24)
            },
            "artist_candidates": {
                "artist": {
                    "payload": {
                        "id": "artist",
                        "name": "Test Artist",
                        "thumbnail": "artist.jpg",
                    },
                    "source_scores": {"fast_artist": 4.0},
                }
            },
            "album_candidates": {},
            "retrieval_diagnostics": {},
        }
        service = SearchService(server)
        first = service.search(
            SimpleNamespace(
                query=query,
                user_scope_id="snapshot-user",
                surface="search",
                force_refresh=False,
                limit=16,
                search_mode="exact",
                defer_side_surfaces=True,
                result_type="",
                offset=0,
            )
        )
        second = service.search(
            SimpleNamespace(
                query=query,
                user_scope_id="snapshot-user",
                surface="search",
                force_refresh=False,
                limit=16,
                search_mode="exact",
                defer_side_surfaces=False,
                result_type="tracks",
                offset=16,
            )
        )

        self.assertEqual(len(first.get("tracks") or []), 16)
        self.assertEqual(len(second.get("tracks") or []), 8)
        self.assertEqual(mock_retrieve.call_count, 1)
        self.assertTrue(
            bool((second.get("diagnostics") or {}).get("search_snapshot_hit"))
        )

    @patch("auralis_backend.search.service.retrieve_search_candidates_fast")
    def test_deferred_artist_surface_expands_after_snapshot_is_consumed(
        self,
        mock_retrieve,
    ) -> None:
        def payload_for(request, *_args, **_kwargs):
            expanded = getattr(request, "result_type", "") == "artists"
            artist_payloads = [
                {
                    "id": "artist-lead",
                    "name": "Lead Artist",
                    "thumbnail": "lead.jpg" if expanded else "",
                }
            ]
            if expanded:
                artist_payloads.extend(
                    [
                        {
                            "id": "artist-related-1",
                            "name": "Related One",
                            "thumbnail": "one.jpg",
                        },
                        {
                            "id": "artist-related-2",
                            "name": "Related Two",
                            "thumbnail": "two.jpg",
                        },
                    ]
                )
            return {
                "query_intent": "artist",
                "normalized_anchor_artists": {"lead artist"},
                "track_candidates": (
                    {
                        "lead-track": {
                            "payload": {
                                "id": "lead-track",
                                "playback": {
                                    "provider": "youtube",
                                    "source_id": "00000000001",
                                },
                                "title": "Lead Song",
                                "channel": "Lead Artist",
                            },
                            "source_scores": {"artist_catalog": 4.0},
                        }
                    }
                    if expanded
                    else {}
                ),
                "artist_candidates": {
                    item["id"]: {
                        "payload": item,
                        "source_scores": {"artists.fast": 4.0},
                    }
                    for item in artist_payloads
                },
                "album_candidates": (
                    {
                        "lead-album": {
                            "payload": {
                                "id": "MPRE-lead-album",
                                "title": "Lead Album",
                                "artist": "Lead Artist",
                            },
                            "source_scores": {"artist_discography": 4.0},
                        }
                    }
                    if expanded
                    else {}
                ),
                "related_artists": (
                    [
                        {
                            "id": "artist-neighbour",
                            "name": "Close Neighbour",
                            "thumbnail": "neighbour.jpg",
                        }
                    ]
                    if expanded
                    else []
                ),
                "retrieval_diagnostics": {},
            }

        mock_retrieve.side_effect = payload_for
        service = SearchService(server)

        def catalog_result(*_args, **_kwargs):
            return {
                "artist": {
                    "id": "artist-lead",
                    "name": "Lead Artist",
                    "thumbnail": "lead.jpg",
                },
                "tracks": [
                    {
                        "id": "lead-track",
                        "playback": {
                            "provider": "youtube",
                            "source_id": "00000000001",
                        },
                        "title": "Lead Song",
                        "channel": "Lead Artist",
                    }
                ],
                "albums": [
                    {
                        "id": "MPRE-lead-album",
                        "title": "Lead Album",
                        "artist": "Lead Artist",
                    }
                ],
                "related_artists": [
                    {
                        "id": "artist-neighbour",
                        "name": "Close Neighbour",
                        "thumbnail": "neighbour.jpg",
                    }
                ],
                "catalog_status": "complete",
                "album_tracklists_loaded": 1,
            }

        with (
            patch(
                "auralis_backend.search.service.load_artist_entity_expansion",
                side_effect=catalog_result,
            ),
            patch.object(
                service,
                "_lastfm_related_artists",
                return_value=[
                    {
                        "id": "musicbrainz:artist:lastfm-neighbour",
                        "name": "Last.fm Neighbour",
                        "relationship_provider": "lastfm",
                        "thumbnail": "lastfm-neighbour.jpg",
                    }
                ],
            ),
            patch.object(
                service,
                "_resolve_first_page_related_artists",
                side_effect=lambda artists, **_kwargs: artists,
            ),
        ):
            first = service.search(
                SimpleNamespace(
                    query="Lead Artist",
                    user_scope_id="progressive-user",
                    surface="search",
                    force_refresh=False,
                    limit=16,
                    search_mode="entity",
                    defer_side_surfaces=True,
                    result_type="",
                    offset=0,
                )
            )
            second = service.search(
                SimpleNamespace(
                    query="Lead Artist",
                    user_scope_id="progressive-user",
                    surface="search",
                    force_refresh=False,
                    limit=16,
                    search_mode="entity",
                    defer_side_surfaces=False,
                    result_type="artists",
                    offset=1,
                )
            )

        self.assertEqual(len(first.get("artists") or []), 1)
        self.assertEqual(mock_retrieve.call_count, 1)
        self.assertEqual(
            (second.get("artists") or [])[0].get("thumbnail"),
            "lead.jpg",
        )
        self.assertEqual(
            [item.get("name") for item in second.get("similar_artists") or []],
            ["Last.fm Neighbour", "Close Neighbour"],
        )
        self.assertEqual(
            [item.get("id") for item in second.get("artist_tracks") or []],
            ["lead-track"],
        )
        self.assertEqual(
            [item.get("id") for item in second.get("artist_albums") or []],
            ["MPRE-lead-album"],
        )
        self.assertEqual(second.get("related_albums") or [], [])
        self.assertFalse(
            bool(
                (second.get("pagination") or {})
                .get("artists", {})
                .get("deferred_expansion")
            )
        )

    @patch("auralis_backend.search.service.retrieve_search_candidates_fast")
    def test_indirect_query_uses_the_same_canonical_search_path(
        self,
        mock_retrieve,
    ) -> None:
        mock_retrieve.return_value = {
            "query_intent": "mixed",
            "normalized_anchor_artists": set(),
            "track_candidates": {},
            "artist_candidates": {},
            "album_candidates": {},
            "retrieval_diagnostics": {},
        }
        response = SearchService(server).search(
            SimpleNamespace(
                query="that song about cavalry by iron maiden",
                user_scope_id="guest",
                surface="search",
                force_refresh=False,
                limit=16,
                search_mode="",
                defer_side_surfaces=True,
            )
        )
        self.assertEqual(response.get("status"), "success")
        self.assertEqual((response.get("diagnostics") or {}).get("query_mode"), "taste")
        self.assertEqual(mock_retrieve.call_count, 1)

    @patch("auralis_backend.search.service.retrieve_search_candidates_fast")
    @patch("auralis_backend.search.service.semantic_search_suggestion_items")
    def test_suggestions_do_not_start_a_second_search(
        self,
        mock_suggestions,
        mock_retrieve,
    ) -> None:
        mock_suggestions.return_value = [
            {"text": "Iron Maiden", "suggestion_type": "artist"}
        ]
        response = SearchService(server).suggest(
            SimpleNamespace(
                query="iron mai",
                user_scope_id="guest",
                limit=5,
            )
        )
        self.assertEqual(response.get("results"), ["Iron Maiden"])
        self.assertFalse(
            bool((response.get("diagnostics") or {}).get("warmup_scheduled"))
        )
        mock_retrieve.assert_not_called()

    @patch("auralis_backend.search.service.search_artists_direct_cached")
    def test_artist_resolver_uses_direct_artist_lookup(self, mock_artist_lookup) -> None:
        mock_artist_lookup.return_value = [
            {"id": "UC-radiohead", "name": "Radiohead"},
            {"id": "UC-other", "name": "Radiohead Tribute"},
        ]
        response = SearchService(server).resolve_artist(
            SimpleNamespace(query="Radiohead", limit=4)
        )
        self.assertEqual(response.get("artist", {}).get("id"), "UC-radiohead")
        self.assertEqual(
            (response.get("diagnostics") or {}).get("ranking_backend"),
            "canonical_artist_resolver_v1",
        )

    @patch("auralis_backend.search.service.retrieve_search_candidates_fast")
    def test_artist_entity_search_uses_evidence_and_skips_profile(
        self,
        mock_retrieve_fast,
    ) -> None:
        mock_retrieve_fast.return_value = {
            "query_intent": "artist",
            "normalized_anchor_artists": {"aerosmith"},
            "track_candidates": {
                "track_1": {
                    "payload": {
                        "id": "track_1",
                        "playback": {"provider": "youtube", "source_id": "00000000001"},
                        "title": "Dream On",
                        "channel": "Aerosmith",
                        "album": "Aerosmith",
                    },
                    "source_scores": {"tracks.fast": 1.0},
                },
            },
            "artist_candidates": {
                "artist_1": {
                    "payload": {"id": "artist_1", "name": "Aerosmith"},
                    "source_scores": {"artists.fast": 1.0},
                },
            },
            "album_candidates": {
                "album_1": {
                    "payload": {
                        "id": "MPRE-album-1",
                        "title": "Aerosmith",
                        "artist": "Aerosmith",
                    },
                    "source_scores": {"albums.fast": 1.0},
                },
            },
            "retriever_counts": {},
            "retrieval_diagnostics": {},
        }
        service = SearchService(server)
        response = service.search(
            SimpleNamespace(
                query="aerosmith",
                user_scope_id="guest",
                surface="search",
                force_refresh=False,
                limit=12,
                defer_side_surfaces=True,
                search_mode="entity",
            )
        )
        self.assertEqual(response.get("status"), "success")
        self.assertEqual(len(list(response.get("tracks") or [])), 1)
        self.assertEqual((response.get("artists") or [])[0].get("name"), "Aerosmith")
        self.assertEqual((response.get("albums") or [])[0].get("title"), "Aerosmith")
        self.assertEqual(response.get("query_intent"), "artist")
        self.assertEqual((response.get("top_result") or {}).get("entity_type"), "artist")
        self.assertTrue(bool(response.get("resolved_entity_key")))
        diagnostics = dict(response.get("diagnostics") or {})
        self.assertEqual(diagnostics.get("ranking_backend"), "canonical_search_v1")
        self.assertTrue(bool(diagnostics.get("profile_build_skipped")))


if __name__ == "__main__":
    unittest.main()
