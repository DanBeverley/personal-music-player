import pathlib
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import pytest

CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import server
from auralis_backend.domain.server_adapter import adapt_domain_server
from auralis_backend.search.intelligence import search_text_similarity
from auralis_backend.search.query_mode import resolve_search_mode
from auralis_backend.search.service import SearchService
from auralis_backend.search.upstream_runtime import normalize_song_result
from auralis_backend.storage.artist_artwork import (
    entity_artwork_identity,
    entity_artwork_token,
)


def _visible_declared_artwork(
    artists,
    excluded_artist=None,
    **_kwargs,
):
    excluded_id = str((excluded_artist or {}).get("id") or "").strip()
    excluded_name = str((excluded_artist or {}).get("name") or "").strip().casefold()
    return [
        dict(artist)
        for artist in artists
        if str((artist or {}).get("thumbnail") or "").startswith(
            ("http://", "https://", "/artist_artwork/")
        )
        and not (
            excluded_id
            and str((artist or {}).get("id") or "").strip() == excluded_id
        )
        and not (
            excluded_name
            and str((artist or {}).get("name") or "").strip().casefold()
            == excluded_name
        )
    ]


def _verified_declared_entity_artwork(_server, item, *, entity_type, **_kwargs):
    updated = dict(item or {})
    source = str(updated.get("thumbnail") or "").strip()
    identity = entity_artwork_identity(updated, entity_type=entity_type)
    token = entity_artwork_token(updated, entity_type=entity_type)
    if source.startswith(("http://", "https://")):
        updated["artwork_source_url"] = source
    updated["artwork_entity_type"] = entity_type
    updated["artwork_cache_identity"] = identity
    updated["artwork_cache_token"] = token
    updated["artwork_cache_status"] = "cached"
    updated["thumbnail"] = f"/entity_artwork/{token}"
    return updated


def _test_resolved_target(
    entity_type,
    item,
    *,
    lead_artist=None,
    containing_album=None,
):
    entity_id = str(
        item.get("videoId")
        or item.get("id")
        or item.get("browseId")
        or ""
    ).strip().casefold()
    return {
        "entity_type": entity_type,
        "item": dict(item),
        "lead_artist": dict(lead_artist or (item if entity_type == "artist" else {})),
        "containing_album": dict(
            containing_album or (item if entity_type == "album" else {})
        ),
        "target_identity": f"provider:{entity_type}:{entity_id}",
        "confidence": 0.9,
        "confidence_tier": "corroborated",
        "decision_margin": 1.0,
        "evidence": ["test_provider_identity"],
        "resolver": "evidence_first_v1",
    }


@pytest.mark.search_core
class SearchLatencyTests(unittest.TestCase):
    def test_incomplete_provider_results_do_not_become_a_complete_direct_cache_hit(self):
        import auralis_backend.search.runtime as runtime
        partial = {'id':'00000000001','title':'Work','search_provider_missing':['videos']}
        with patch.object(runtime,'lookup_search_result',return_value=[partial]), \
             patch.object(runtime,'store_search_result') as store, \
             patch.object(runtime,'resolve_ytmusic_song_search',return_value=[partial]) as acquire:
            runtime.search_tracks_direct('Partial work',16,server=server,first_page_only=True)
        acquire.assert_called_once()
        store.assert_not_called()



    def test_full_songs_results_retain_video_candidates_until_ranking(self):
        import auralis_backend.search.runtime as runtime
        rows = [{'id':f'{i:011d}','title':'Work','channel':'Artist'} for i in range(48)]
        with patch.object(runtime,'lookup_search_result',return_value=None), \
             patch.object(runtime,'store_search_result'), \
             patch.object(runtime,'resolve_ytmusic_song_search',return_value=rows):
            result = runtime.search_tracks_direct('Full quality work',24,server=server)
        self.assertEqual(len(result),48)
        self.assertEqual(result[-1]['id'],rows[-1]['id'])

    def test_cancelled_running_provider_call_keeps_its_admission_slot(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import BoundedSemaphore, Event
        from auralis_backend.search.server_adapter import SearchServerAdapter
        started, release = Event(), Event()
        admission = BoundedSemaphore(1)
        def work():
            started.set()
            release.wait(timeout=.5)
        with ThreadPoolExecutor(max_workers=1) as executor:
            adapter = SearchServerAdapter(SimpleNamespace(search_upstream_executor=executor,
                search_upstream_admission=admission))
            future = adapter.submit_search_upstream(work,deadline=time.monotonic()+1)
            try:
                self.assertTrue(started.wait(timeout=.2))
                self.assertFalse(future.cancel())
                self.assertIsNone(adapter.submit_search_upstream(lambda:None,deadline=time.monotonic()+.1))
            finally:
                release.set()
            future.result(timeout=1)
        self.assertTrue(admission.acquire(blocking=False))
        admission.release()





    def test_core_artwork_scheduling_avoids_remote_head_on_the_response_thread(self):
        import auralis_backend.storage.artist_artwork as artwork
        artist = {'id':'UC-one','name':'One','thumbnail':'/artist_artwork/verified'}
        with patch.object(artwork,'attach_persisted_artist_artwork',return_value=artist), \
             patch.object(artwork,'attach_cached_artist_artwork',side_effect=AssertionError('remote HEAD')), \
             patch.object(artwork,'notify_artist_metadata_updated'):
            self.assertTrue(artwork.schedule_artist_artwork_cache(server,artist,local_only=True))


    def test_weak_song_shelf_adds_typed_video_coverage_with_shared_deadline(self):
        from auralis_backend.search.upstream_runtime import ytmusic_song_search
        from auralis_backend.search.server_adapter import SearchServerAdapter
        from concurrent.futures import ThreadPoolExecutor
        from threading import BoundedSemaphore, Barrier
        from unittest.mock import Mock
        upstream = Mock()
        primary = [{'id': str(i), 'title': 'Bury the Light', 'views': 100} for i in range(25)]
        wanted = {'id': 'wanted', 'title': 'Bury the Light - soundtrack', 'resultType': 'video', 'views': 33000000}
        barrier = Barrier(2)
        deadlines = []
        def lookup(query, *, filter, limit):
            from auralis_backend.search.upstream_runtime import _search_http_deadline
            deadlines.append(_search_http_deadline.get())
            barrier.wait(timeout=1)
            return primary if filter == 'songs' else [wanted]
        upstream.search.side_effect = lookup
        with ThreadPoolExecutor(max_workers=2) as executor:
            adapter = SearchServerAdapter(SimpleNamespace(ytmusic=upstream,
                search_upstream_executor=executor, search_upstream_admission=BoundedSemaphore(2)))
            with patch('auralis_backend.search.upstream_runtime.adapt_search_server', return_value=adapter), \
                 patch('auralis_backend.search.upstream_runtime.normalize_song_result', side_effect=lambda _, item: dict(item)):
                results = ytmusic_song_search(adapter, 'Bury the Light', 16, first_page_only=True)
        self.assertEqual(results[1]['id'], wanted['id'])
        self.assertLessEqual(len(results), 32)
        self.assertEqual({c.kwargs['filter'] for c in upstream.search.call_args_list}, {'songs', 'videos'})
        self.assertEqual(deadlines[0], deadlines[1])

    def test_exhausted_song_budget_never_starts_video_request(self):
        from auralis_backend.search.upstream_runtime import ytmusic_song_search
        from auralis_backend.search.server_adapter import SearchServerAdapter
        from itertools import chain, repeat
        from unittest.mock import Mock
        upstream = Mock(search=Mock(return_value=[]))
        adapter = SearchServerAdapter(SimpleNamespace(ytmusic=upstream))
        with patch('auralis_backend.search.upstream_runtime.adapt_search_server', return_value=adapter), \
             patch('auralis_backend.search.upstream_runtime.time.monotonic', side_effect=chain([10], repeat(12.1))):
            with self.assertRaises(TimeoutError):
                ytmusic_song_search(adapter, 'unseen query', 16, first_page_only=True)
        self.assertEqual(upstream.search.call_count, 0)






    def test_preview_compares_both_typed_shelves_without_broad_refill(self):
        from auralis_backend.search.upstream_runtime import ytmusic_song_search
        from auralis_backend.search.server_adapter import SearchServerAdapter
        from unittest.mock import Mock
        upstream = Mock()
        upstream.search.return_value = [{'id': str(i), 'title': 'Fresh Query', 'views': 1000000} for i in range(5)]
        adapter = SearchServerAdapter(SimpleNamespace(ytmusic=upstream,
            search_upstream_executor=server.search_upstream_executor,
            search_upstream_admission=server.search_upstream_admission))
        with patch('auralis_backend.search.upstream_runtime.adapt_search_server', return_value=adapter), \
             patch('auralis_backend.search.upstream_runtime.normalize_song_result', side_effect=lambda _, item: dict(item)):
            result = ytmusic_song_search(adapter, 'fresh query', 16, first_page_only=True)
            self.assertEqual(len(result), 5)
            self.assertEqual(upstream.search.call_count, 2)
            ytmusic_song_search(adapter, 'fresh query', 16)
            self.assertEqual(upstream.search.call_count, 4)

    def test_preview_retains_empty_primary_fallback(self):
        from auralis_backend.search.upstream_runtime import ytmusic_song_search
        from auralis_backend.search.server_adapter import SearchServerAdapter
        from unittest.mock import Mock
        upstream = Mock()
        upstream.search.side_effect = lambda query, **kw: ([] if kw['filter'] == 'songs' else
            [{'id': 'source', 'title': 'Recovered', 'resultType': 'song'}])
        adapter = SearchServerAdapter(SimpleNamespace(ytmusic=upstream,
            search_upstream_executor=server.search_upstream_executor,
            search_upstream_admission=server.search_upstream_admission))
        with patch('auralis_backend.search.upstream_runtime.adapt_search_server', return_value=adapter), \
             patch('auralis_backend.search.upstream_runtime.normalize_song_result', side_effect=lambda _, item: item):
            result = ytmusic_song_search(adapter, 'fresh query', 16, first_page_only=True)
            self.assertEqual(result[0]['id'], 'source')
            self.assertEqual(upstream.search.call_count, 2)








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






    @pytest.mark.search_artwork
    def test_album_artwork_identity_prefers_provider_detail_id_across_aliases(self) -> None:
        search_album = {
            "id": "MPRE-shared-album",
            "provider_album_id": "MPRE-shared-album",
            "canonical_album_identity": "musicbrainz:release-group:rg-1",
            "musicbrainz_release_group_id": "rg-1",
            "title": "Shared Album",
            "artist": "Test Artist",
        }
        detail_album = {
            "id": "MPRE-shared-album",
            "title": "Shared Album",
            "artist": "Test Artist",
        }

        self.assertEqual(
            entity_artwork_token(search_album, entity_type="album"),
            entity_artwork_token(detail_album, entity_type="album"),
        )

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





    def test_partial_and_reordered_text_match_canonical_title(self) -> None:
        self.assertGreaterEqual(
            search_text_similarity("water smoke", "Smoke on the Water"),
            0.75,
        )
        self.assertGreaterEqual(
            search_text_similarity("bohemain rapsody", "Bohemian Rhapsody"),
            0.75,
        )




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





if __name__ == "__main__":
    unittest.main()
