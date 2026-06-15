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
    _rich_retrieval_budget_plan,
    retrieve_search_candidates_fast,
)
from auralis_backend.search.pipeline import _search_ranking_budget_plan
from auralis_backend.search.service import SearchService


class SearchLatencyTests(unittest.TestCase):
    def test_two_word_song_title_is_eligible_for_direct_track_path(self) -> None:
        service = SearchService(server)
        self.assertTrue(
            service._should_try_direct_track_path(
                "Purple Rain",
                intent_hint="mixed",
            )
        )

    def test_search_ranking_budget_plan_tightens_entity_mode(self) -> None:
        entity_plan = _search_ranking_budget_plan(
            search_mode="entity",
            query_intent="artist",
            limit=12,
        )
        taste_plan = _search_ranking_budget_plan(
            search_mode="taste",
            query_intent="mood",
            limit=12,
        )
        self.assertLess(
            int(entity_plan.get("track_candidate_cap") or 0),
            int(taste_plan.get("track_candidate_cap") or 0),
        )
        self.assertLess(
            int(entity_plan.get("artist_candidate_cap") or 0),
            int(taste_plan.get("artist_candidate_cap") or 0),
        )
        self.assertLessEqual(
            int(entity_plan.get("artist_output_limit") or 0),
            int(taste_plan.get("artist_output_limit") or 0),
        )

    def test_rich_retrieval_budget_plan_tightens_entity_mode(self) -> None:
        entity_plan = _rich_retrieval_budget_plan("entity", limit=12)
        taste_plan = _rich_retrieval_budget_plan("taste", limit=12)
        self.assertLess(
            float(entity_plan.get("total_budget_seconds") or 0.0),
            float(taste_plan.get("total_budget_seconds") or 0.0),
        )
        self.assertLess(
            int(entity_plan.get("anchor_neighbor_count") or 0),
            int(taste_plan.get("anchor_neighbor_count") or 0),
        )
        self.assertLess(
            int(entity_plan.get("artist_seed_count") or 0),
            int(taste_plan.get("artist_seed_count") or 0),
        )

    def test_rich_retrieval_budget_plan_keeps_taste_mode_broader(self) -> None:
        taste_plan = _rich_retrieval_budget_plan("taste", limit=12)
        self.assertEqual(taste_plan.get("mode"), "taste")
        self.assertGreaterEqual(
            int(taste_plan.get("context_query_count") or 0),
            2,
        )
        self.assertGreaterEqual(
            int(taste_plan.get("context_artist_count") or 0),
            6,
        )

    @patch("auralis_backend.domain.retrieval._retrieval_cache_set")
    @patch("auralis_backend.domain.retrieval._retrieval_cache_get", return_value=None)
    @patch("auralis_backend.domain.retrieval.search_albums_blended", side_effect=AssertionError("blended albums should not run"))
    @patch("auralis_backend.domain.retrieval.search_tracks_blended", side_effect=AssertionError("blended tracks should not run"))
    @patch("auralis_backend.domain.retrieval.search_albums_direct")
    @patch("auralis_backend.domain.retrieval.search_artists_direct_cached")
    @patch("auralis_backend.domain.retrieval.search_tracks_direct")
    def test_fast_retrieval_uses_direct_search_helpers(
        self,
        mock_tracks_direct,
        mock_artists_direct,
        mock_albums_direct,
        _mock_tracks_blended,
        _mock_albums_blended,
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

    @patch("auralis_backend.search.service.search_albums_direct", return_value=[])
    @patch("auralis_backend.search.service.search_artists_direct_cached", return_value=[])
    @patch("auralis_backend.search.service.search_tracks_direct")
    @patch.object(SearchService, "_direct_track_match_score", return_value=1.0)
    @patch.object(SearchService, "_should_try_direct_track_path", return_value=True)
    @patch("auralis_backend.search.service.SEARCH_DISABLE_RANKING_PIPELINE", False)
    @patch("auralis_backend.search.service.build_search_profile")
    def test_exact_track_query_uses_direct_fast_path_before_profile_build(
        self,
        mock_build_profile,
        _mock_should_try_direct,
        _mock_direct_match_score,
        mock_search_tracks_direct,
        _mock_search_artists,
        _mock_search_albums,
    ) -> None:
        mock_build_profile.return_value = (
            SimpleNamespace(query="stairway to heaven"),
            {
                "profile_key": "test-profile",
                "profile_runtime": {},
                "catalog_feature_version": "",
                "taste_profile_version": "",
                "scene_graph_version": "",
                "feature_source": "",
                "negative_feedback_applied": False,
            },
        )
        mock_search_tracks_direct.return_value = [
            {
                "id": "track_1",
                "title": "Stairway to Heaven",
                "channel": "Led Zeppelin",
                "album": "Led Zeppelin IV",
            }
        ]
        service = SearchService(server)
        response = service.search(
            SimpleNamespace(
                query="stairway to heaven",
                user_scope_id="guest",
                surface="search",
                force_refresh=False,
                limit=16,
            )
        )
        self.assertEqual(response.get("status"), "success")
        self.assertEqual(response.get("query_intent"), "track")
        diagnostics = dict(response.get("diagnostics") or {})
        self.assertEqual(diagnostics.get("ranking_backend"), "search_service_direct_v1")
        self.assertTrue(bool(diagnostics.get("direct_track_fast_path")))
        mock_build_profile.assert_not_called()

    @patch("auralis_backend.search.service.search_albums_direct", return_value=[])
    @patch("auralis_backend.search.service.search_artists_direct_cached", return_value=[])
    @patch("auralis_backend.search.service.search_tracks_direct")
    @patch.object(SearchService, "_direct_track_match_score", return_value=1.0)
    @patch.object(SearchService, "_should_try_direct_track_path", return_value=False)
    @patch("auralis_backend.search.service.build_search_profile")
    def test_two_word_exact_query_uses_direct_path_without_profile_build(
        self,
        mock_build_profile,
        _mock_should_try_direct,
        _mock_direct_match_score,
        mock_search_tracks_direct,
        _mock_search_artists,
        _mock_search_albums,
    ) -> None:
        mock_search_tracks_direct.return_value = [
            {
                "id": "purple-rain",
                "title": "Purple Rain",
                "channel": "Prince",
                "album": "Purple Rain",
            }
        ]
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
        mock_build_profile.assert_not_called()

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

    @patch.object(server, "_build_artist_details_payload")
    @patch("auralis_backend.search.service.search_albums_direct")
    @patch("auralis_backend.search.service.search_artists_direct_cached")
    @patch("auralis_backend.search.service.search_tracks_direct")
    @patch("auralis_backend.search.service.build_search_profile")
    def test_direct_search_mode_bypasses_ranking_pipeline(
        self,
        mock_build_profile,
        mock_search_tracks_direct,
        mock_search_artists_direct,
        mock_search_albums_direct,
        mock_build_artist_details,
    ) -> None:
        mock_search_tracks_direct.return_value = [
            {
                "id": "track_1",
                "title": "Stairway to Heaven",
                "channel": "Led Zeppelin",
                "album": "Led Zeppelin IV",
            }
        ]
        mock_search_artists_direct.return_value = [{"id": "artist_1", "name": "Led Zeppelin"}]
        mock_search_albums_direct.return_value = [{"id": "album_1", "title": "Led Zeppelin IV"}]
        mock_build_artist_details.return_value = {
            "related_artists": [
                {"id": "artist_2", "name": "The Who"},
                {"id": "artist_3", "name": "Deep Purple"},
            ]
        }
        service = SearchService(server)
        response = service.search(
            SimpleNamespace(
                query="stairway to heaven",
                user_scope_id="guest",
                surface="search",
                force_refresh=False,
                limit=16,
            )
        )
        self.assertEqual(response.get("status"), "success")
        diagnostics = dict(response.get("diagnostics") or {})
        self.assertEqual(diagnostics.get("ranking_backend"), "search_service_direct_v1")
        self.assertTrue(bool(diagnostics.get("direct_track_fast_path")))
        mock_build_profile.assert_not_called()
        self.assertEqual("Led Zeppelin", response.get("artists")[0].get("name"))
        self.assertEqual("Led Zeppelin IV", response.get("albums")[0].get("title"))
        self.assertEqual("The Who", response.get("similar_artists")[0].get("name"))

    @patch.object(server, "_build_artist_details_payload")
    @patch("auralis_backend.search.service.search_albums_direct")
    @patch("auralis_backend.search.service.search_artists_direct_cached")
    @patch("auralis_backend.search.service.search_tracks_direct")
    @patch("auralis_backend.search.service.build_search_profile")
    def test_direct_search_tracks_first_defers_side_surfaces(
        self,
        mock_build_profile,
        mock_search_tracks_direct,
        mock_search_artists_direct,
        mock_search_albums_direct,
        mock_build_artist_details,
    ) -> None:
        mock_search_tracks_direct.return_value = [
            {
                "id": "track_1",
                "title": "Stairway to Heaven",
                "channel": "Led Zeppelin",
                "album": "Led Zeppelin IV",
            }
        ]
        mock_search_artists_direct.return_value = [{"id": "artist_1", "name": "Led Zeppelin"}]
        mock_search_albums_direct.return_value = [{"id": "album_1", "title": "Led Zeppelin IV"}]
        mock_build_artist_details.return_value = {
            "related_artists": [
                {"id": "artist_2", "name": "The Who"},
            ]
        }
        service = SearchService(server)
        response = service.search(
            SimpleNamespace(
                query="stairway to heaven",
                user_scope_id="guest",
                surface="search",
                force_refresh=False,
                limit=16,
                defer_side_surfaces=True,
            )
        )
        self.assertEqual(response.get("status"), "success")
        self.assertEqual(len(list(response.get("tracks") or [])), 1)
        self.assertEqual((response.get("artists") or [])[0].get("name"), "Led Zeppelin")
        self.assertEqual((response.get("albums") or [])[0].get("title"), "Led Zeppelin IV")
        self.assertEqual(list(response.get("similar_artists") or []), [])
        diagnostics = dict(response.get("diagnostics") or {})
        self.assertTrue(bool(diagnostics.get("deferred_side_surfaces")))
        mock_build_profile.assert_not_called()
        mock_build_artist_details.assert_not_called()

    @patch("auralis_backend.search.service.DIRECT_SIDE_SURFACE_BUDGET_SECONDS", 0.03)
    @patch("auralis_backend.search.service.search_albums_direct")
    @patch("auralis_backend.search.service.search_artists_direct_cached")
    @patch("auralis_backend.search.service.search_tracks_direct", return_value=[])
    def test_direct_side_surfaces_return_partial_results_within_budget(
        self,
        _mock_search_tracks,
        mock_search_artists,
        mock_search_albums,
    ) -> None:
        def slow_artist_search(*_args, **_kwargs):
            time.sleep(0.15)
            return [{"id": "artist_1", "name": "Slow Artist"}]

        mock_search_artists.side_effect = slow_artist_search
        mock_search_albums.return_value = [
            {"id": "album_1", "title": "Ready Album", "artist": "Ready Artist"},
        ]
        trace = server._trace_start(
            "search",
            user_scope_id="guest",
            surface="search",
            query="ready album",
        )
        trace["started_at_perf"] = time.perf_counter()

        started_at = time.perf_counter()
        response = SearchService(server)._search_without_ranking(
            req=SimpleNamespace(
                query="ready album",
                user_scope_id="guest",
                search_mode="exact",
                defer_side_surfaces=False,
            ),
            trace=trace,
            query="ready album",
            limit=16,
            query_intent="album",
            track_model_version="test",
        )

        self.assertLess(time.perf_counter() - started_at, 0.12)
        self.assertEqual(response.get("artists"), [])
        self.assertEqual(response.get("albums")[0].get("id"), "album_1")

    @patch("auralis_backend.search.service.get_search_snapshot_for_profile", return_value=None)
    @patch("auralis_backend.search.service.get_search_snapshot", return_value=None)
    @patch("auralis_backend.search.service.retrieve_search_candidates_fast")
    @patch("auralis_backend.search.service.build_search_profile")
    @patch("auralis_backend.search.service.SEARCH_DISABLE_RANKING_PIPELINE", False)
    @patch.object(SearchService, "_should_try_direct_track_path", return_value=False)
    @patch.object(
        SearchService,
        "_rank_album_candidates",
        side_effect=AssertionError("album reranker should not run"),
    )
    @patch.object(
        SearchService,
        "_rank_artist_candidates",
        side_effect=AssertionError("artist reranker should not run"),
    )
    @patch.object(SearchService, "_rank_track_candidates")
    def test_ranked_search_derives_side_surfaces_without_running_side_rerank(
        self,
        mock_rank_tracks,
        _mock_rank_artists,
        _mock_rank_albums,
        _mock_should_try_direct,
        mock_build_profile,
        mock_retrieve_fast,
        _mock_snapshot_get,
        _mock_snapshot_get_profile,
    ) -> None:
        mock_build_profile.return_value = (
            SimpleNamespace(query="aerosmith"),
            {
                "profile_key": "test-profile",
                "profile_runtime": {},
                "catalog_feature_version": "",
                "taste_profile_version": "",
                "scene_graph_version": "",
                "feature_source": "",
                "negative_feedback_applied": False,
            },
        )
        mock_retrieve_fast.return_value = {
            "query_intent": "artist",
            "track_candidates": {
                "track_1": {
                    "payload": {
                        "id": "track_1",
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
                    "payload": {"id": "album_1", "title": "Aerosmith", "artist": "Aerosmith"},
                    "source_scores": {"albums.fast": 1.0},
                },
            },
            "retriever_counts": {},
            "retrieval_diagnostics": {},
        }
        mock_rank_tracks.return_value = [
            {
                "id": "track_1",
                "title": "Dream On",
                "channel": "Aerosmith",
                "album": "Aerosmith",
                "score": 1.0,
            }
        ]
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
        diagnostics = dict(response.get("diagnostics") or {})
        self.assertEqual((diagnostics.get("ranking_budget") or {}).get("mode"), "entity")
        self.assertTrue(bool(diagnostics.get("deferred_side_surfaces")))
        mock_rank_tracks.assert_called_once()


if __name__ == "__main__":
    unittest.main()
