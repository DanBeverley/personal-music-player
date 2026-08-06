import json
import pathlib
import re
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock, patch

CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import server
from auralis_backend.search.runtime import (
    search_canonical_album_for_track,
    semantic_search_suggestion_items,
)
from auralis_backend.search.service import SearchService
from auralis_backend.search import service as search_service_module
from auralis_backend.search.canonical import resolve_canonical_tracks, source_quality_score
from auralis_backend.search.catalog_pipeline import (
    catalog_album_is_detail_ready,
    catalog_import_coverage_report,
    catalog_playable_tracks_for_query,
    collect_external_catalog_backfill_seeds,
    enqueue_external_catalog_seeds,
    external_catalog_import_progress,
    populate_catalog_from_user_signals,
    run_external_catalog_import,
    schedule_catalog_population,
)
from auralis_backend.search.intelligence import (
    annotate_canonical_entity,
    annotate_source_identity,
    backfill_canonical_catalog,
    catalog_entity_key,
    enrich_query_with_musicbrainz,
    load_catalog_artist_records,
    load_catalog_entity_memories,
    load_fuzzy_catalog_entity_memories,
    load_canonical_entity,
    load_query_aliases,
    load_query_memory,
    remember_candidate_observations,
    remember_catalog_entity,
    remember_search_resolution,
    remember_source_identities,
    remember_source_identity,
    remove_unconfirmed_display_memories,
    remove_untrusted_catalog_query_aliases,
)
from auralis_backend.storage.artist_artwork import (
    attach_cached_artist_artwork,
    artist_artwork_path,
    artist_artwork_token,
    schedule_artist_artwork_cache,
)
from auralis_backend.search.musicbrainz import (
    musicbrainz_artist_to_item,
    musicbrainz_recording_to_item,
    musicbrainz_release_group_to_item,
    search_musicbrainz_recording_items,
)
from auralis_backend.recommend.store_runtime import open_recommendation_store_connection
from auralis_backend.search.server_adapter import SearchServerAdapter
from auralis_backend.domain.catalog import normalized_artist_payload
from auralis_backend.domain.result_quality import album_result_penalty


class _MemoryTestServer:
    def __init__(self, db_path: str) -> None:
        self.RECOMMENDATION_STORE_DB_PATH = db_path
        self.recommendation_store_lock = Lock()

    def _normalize_text(self, value) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())

    def _query_tokens(self, query: str):
        return [
            token
            for token in re.split(r"[^a-z0-9]+", self._normalize_text(query))
            if len(token) >= 3
        ]


class SearchEnrichmentTests(unittest.TestCase):
    def test_short_alias_lookup_is_not_starved_by_internal_substrings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_server = _MemoryTestServer(
                str(pathlib.Path(temp_dir) / "short-alias.sqlite")
            )
            connection = open_recommendation_store_connection(memory_server)
            now = time.time()
            for index in range(1250):
                entity_key = f"provider:artist:radio-studio-{index}"
                connection.execute(
                    """
                    INSERT OR REPLACE INTO catalog_entities(
                        entity_type, entity_key, display_title, display_artist,
                        display_album, confidence, popularity,
                        learned_popularity, payload_json, updated_at
                    ) VALUES('artist', ?, '', ?, '', 1, 1, 1, ?, ?)
                    """,
                    [
                        entity_key,
                        f"Radio Studio {index}",
                        json.dumps(
                            {
                                "id": f"UC-Radio-Studio-{index}",
                                "name": f"Radio Studio {index}",
                            }
                        ),
                        now,
                    ],
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO catalog_entity_aliases(
                        alias_key, entity_type, entity_key, score,
                        confidence, source, updated_at
                    ) VALUES(?, 'artist', ?, 100, 1, 'fixture', ?)
                    """,
                    [f"radio studio {index}", entity_key, now],
                )

            fixtures = [
                (
                    "artist",
                    "provider:artist:dio",
                    "",
                    "Dio",
                    {"id": "UC-Dio", "name": "Dio"},
                ),
                (
                    "track",
                    "provider:track:holy-diver",
                    "Holy Diver",
                    "Dio",
                    {
                        "id": "holy-diver",
                        "title": "Holy Diver",
                        "channel": "Dio",
                        "playback": {
                            "provider": "youtube",
                            "source_id": "holy-diver",
                        },
                    },
                ),
                (
                    "track",
                    "provider:track:dio-tameer",
                    "Dio",
                    "Tameer Hassan",
                    {
                        "id": "dio-tameer",
                        "title": "Dio",
                        "channel": "Tameer Hassan",
                    },
                ),
            ]
            for entity_type, entity_key, title, artist, payload in fixtures:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO catalog_entities(
                        entity_type, entity_key, display_title, display_artist,
                        display_album, confidence, popularity,
                        learned_popularity, payload_json, updated_at
                    ) VALUES(?, ?, ?, ?, '', 1, 1, 1, ?, ?)
                    """,
                    [
                        entity_type,
                        entity_key,
                        title,
                        artist,
                        json.dumps(payload),
                        now,
                    ],
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO catalog_entity_aliases(
                        alias_key, entity_type, entity_key, score,
                        confidence, source, updated_at
                    ) VALUES('dio', ?, ?, 1, 1, 'fixture', ?)
                    """,
                    [entity_type, entity_key, now],
                )
            connection.commit()
            connection.close()

            memories = load_fuzzy_catalog_entity_memories(
                memory_server,
                query="dio",
                limit=12,
                scan_limit=1200,
            )

        identities = {
            (memory.get("entity_type"), memory.get("entity_key"))
            for memory in memories
        }
        self.assertIn(("artist", "provider:artist:dio"), identities)
        self.assertIn(("track", "provider:track:holy-diver"), identities)
        self.assertIn(("track", "provider:track:dio-tameer"), identities)
        self.assertFalse(
            any("radio-studio" in str(entity_key) for _, entity_key in identities)
        )

    def test_automatic_resolution_memory_is_removed_without_user_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_server = _MemoryTestServer(
                str(pathlib.Path(temp_dir) / "automatic-resolution.sqlite")
            )
            self.assertTrue(
                remember_search_resolution(
                    memory_server,
                    user_scope_id="guest",
                    query="dio",
                    entity_type="track",
                    item={
                        "id": "dio-tameer",
                        "title": "Dio",
                        "artist": "Tameer Hassan",
                    },
                    confidence=0.99,
                    event_weight=0.1,
                    event_type="search_resolution",
                    source="canonical_search_response",
                )
            )
            self.assertTrue(
                load_query_memory(
                    memory_server,
                    user_scope_id="guest",
                    query="dio",
                )
            )

            removed = remove_unconfirmed_display_memories(
                memory_server,
                query="dio",
            )

            self.assertGreater(removed, 0)
            self.assertEqual(
                load_query_memory(
                    memory_server,
                    user_scope_id="guest",
                    query="dio",
                ),
                [],
            )

    def test_fuzzy_catalog_lookup_filters_by_query_before_popularity_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_server = _MemoryTestServer(
                str(pathlib.Path(temp_dir) / "catalog.sqlite")
            )
            connection = open_recommendation_store_connection(memory_server)
            now = time.time()
            for index in range(1225):
                entity_key = f"provider:artist:unrelated-{index}"
                connection.execute(
                    """
                    INSERT OR REPLACE INTO catalog_entities(
                        entity_type, entity_key, display_title, display_artist,
                        display_album, confidence, popularity,
                        learned_popularity, payload_json, updated_at
                    ) VALUES('artist', ?, '', ?, '', 1, 1, 1, ?, ?)
                    """,
                    [
                        entity_key,
                        f"Popular Unrelated {index}",
                        json.dumps(
                            {
                                "id": f"UC-Unrelated-{index}",
                                "name": f"Popular Unrelated {index}",
                            }
                        ),
                        now,
                    ],
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO catalog_entity_aliases(
                        alias_key, entity_type, entity_key, score,
                        confidence, source, updated_at
                    ) VALUES(?, 'artist', ?, 100, 1, 'test', ?)
                    """,
                    [f"popular unrelated {index}", entity_key, now],
                )
            target_key = "provider:artist:eric-clapton"
            connection.execute(
                """
                INSERT OR REPLACE INTO catalog_entities(
                    entity_type, entity_key, display_title, display_artist,
                    display_album, confidence, popularity,
                    learned_popularity, payload_json, updated_at
                ) VALUES('artist', ?, '', 'Eric Clapton', '', 0.8, 0, 0, ?, ?)
                """,
                [
                    target_key,
                    json.dumps({"id": "UC-EricClapton", "name": "Eric Clapton"}),
                    now,
                ],
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO catalog_entity_aliases(
                    alias_key, entity_type, entity_key, score,
                    confidence, source, updated_at
                ) VALUES('eric clapton', 'artist', ?, 0.1, 0.8, 'test', ?)
                """,
                [target_key, now],
            )
            connection.commit()
            connection.close()

            memories = load_fuzzy_catalog_entity_memories(
                memory_server,
                query="Eric Clapton",
                entity_type="artist",
                limit=8,
                scan_limit=1200,
            )

        self.assertEqual(len(memories), 1)
        self.assertEqual(
            (memories[0].get("payload") or {}).get("id"),
            "UC-EricClapton",
        )

    def test_provider_album_stays_detail_ready_after_canonical_enrichment(self) -> None:
        self.assertTrue(
            catalog_album_is_detail_ready(
                {
                    "id": "MPREb_everlong",
                    "title": "The Colour and the Shape",
                    "artist": "Foo Fighters",
                    "musicbrainz_release_group_id": "release-group-everlong",
                    "playable": False,
                }
            )
        )

    def test_unprepared_musicbrainz_album_is_not_detail_ready(self) -> None:
        self.assertFalse(
            catalog_album_is_detail_ready(
                {
                    "id": "musicbrainz:release-group:release-id",
                    "musicbrainz_release_group_id": "release-id",
                    "title": "Flight of Icarus",
                    "artist": "Iron Maiden",
                }
            )
        )

    def test_prepared_musicbrainz_album_requires_track_keys(self) -> None:
        self.assertTrue(
            catalog_album_is_detail_ready(
                {
                    "id": "musicbrainz:release-group:release-id",
                    "musicbrainz_release_group_id": "release-id",
                    "playable": True,
                    "tracks": [
                        {
                            "track_key": "recording:recording-id",
                            "playable": True,
                        }
                    ],
                }
            )
        )

    def test_artist_merge_collapses_raw_and_canonical_name_keys(self) -> None:
        merged = SearchService._merge_snapshot_items(
            "artists",
            [
                {
                    "id": "UC-ArcticMonkeys",
                    "name": "Arctic Monkeys",
                }
            ],
            [
                {
                    "id": "UC-ArcticMonkeys",
                    "name": "Arctic Monkeys",
                    "canonical_artist_id": "artist-name:arctic monkeys",
                    "thumbnail": "/artist_artwork/arctic",
                }
            ],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].get("thumbnail"), "/artist_artwork/arctic")

    def test_artist_merge_keeps_homonymous_provider_identities_distinct(self) -> None:
        merged = SearchService._merge_snapshot_items(
            "artists",
            [{"id": "UC-main-nirvana", "name": "Nirvana"}],
            [{"id": "UC-obscure-nirvana", "name": "Nirvana"}],
        )

        self.assertEqual(
            [item.get("id") for item in merged],
            ["UC-main-nirvana", "UC-obscure-nirvana"],
        )

    def test_artist_catalog_rejoins_derived_provider_id_by_canonical_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(
                str(pathlib.Path(tmp_dir) / "artists.sqlite")
            )
            remembered = {
                "id": "UC-canonical-arctic-monkeys",
                "name": "Arctic Monkeys",
                "thumbnail": artist_artwork_path(
                    "provider:artist:uc-canonical-arctic-monkeys"
                ),
                "canonical_artist_id": "artist-name:arctic monkeys",
                "source_authority": "ytmusic_artist_detail",
            }
            self.assertTrue(
                remember_catalog_entity(
                    memory_server,
                    user_scope_id="global",
                    query="Arctic Monkeys",
                    entity_type="artist",
                    item=remembered,
                    confidence=0.98,
                    event_weight=0.0,
                    event_type="artist_metadata",
                    source="test",
                )
            )
            loaded = load_catalog_artist_records(
                memory_server,
                artist_names=["ARCTIC MONKEYS"],
            )
            self.assertEqual(
                loaded["arctic monkeys"].get("id"),
                "UC-canonical-arctic-monkeys",
            )

            service = SearchService(memory_server)
            with (
                patch(
                    "auralis_backend.search.service.load_catalog_artist_payloads",
                    return_value={},
                ),
                patch(
                    "auralis_backend.search.service._SEARCH_CATALOG_WRITER.submit"
                ),
                patch(
                    "auralis_backend.storage.artist_artwork.get_artist_artwork_cache",
                    return_value=SimpleNamespace(
                        head=Mock(return_value={"content_length": 100})
                    ),
                ),
            ):
                hydrated = service._hydrate_artist_artwork(
                    [
                        {
                            "id": "derived:track-uploader",
                            "name": "Arctic Monkeys",
                            "resolution_status": "derived_from_track",
                        }
                    ],
                    allow_live_lead_lookup=False,
                )
            self.assertEqual(
                hydrated[0].get("id"),
                "UC-canonical-arctic-monkeys",
            )
            self.assertEqual(
                hydrated[0].get("thumbnail"),
                artist_artwork_path(
                    "provider:artist:uc-canonical-arctic-monkeys"
                ),
            )

    def test_artist_normalization_preserves_explicit_provider_identity(self) -> None:
        artist = normalized_artist_payload(
            {
                "id": "musicbrainz:artist:mbid-queensryche",
                "provider_artist_id": "UC-Queensryche",
                "musicbrainz_artist_id": "mbid-queensryche",
                "name": "Queensrÿche",
                "artist_aliases": ["Queensryche"],
            }
        )

        self.assertEqual(artist.get("id"), "UC-Queensryche")
        self.assertEqual(
            artist.get("provider_artist_id"),
            "UC-Queensryche",
        )
        self.assertEqual(
            artist.get("canonical_artist_id"),
            "musicbrainz:artist:mbid-queensryche",
        )
        self.assertIn("queensryche", artist.get("artist_aliases") or [])

    def test_accepted_artist_bridge_coalesces_provider_catalog_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(
                str(pathlib.Path(tmp_dir) / "artist-bridge.sqlite")
            )
            cached_path = artist_artwork_path(
                "provider:artist:uc-queensryche"
            )
            provider_record = {
                "id": "UC-Queensryche",
                "provider_artist_id": "UC-Queensryche",
                "name": "Queensrÿche",
                "thumbnail": cached_path,
                "source_authority": "ytmusic_artist_detail",
            }
            self.assertTrue(
                remember_catalog_entity(
                    memory_server,
                    user_scope_id="global",
                    query="Queensrÿche",
                    entity_type="artist",
                    item=provider_record,
                    confidence=0.88,
                    event_weight=0.0,
                    event_type="artist_metadata",
                    source="canonical_search_artist",
                )
            )
            self.assertTrue(
                remember_catalog_entity(
                    memory_server,
                    user_scope_id="global",
                    query="Queensrÿche",
                    entity_type="artist",
                    item={
                        "id": "musicbrainz:artist:mbid-queensryche",
                        "musicbrainz_artist_id": "mbid-queensryche",
                        "name": "Queensrÿche",
                    },
                    confidence=0.98,
                    event_weight=0.0,
                    event_type="musicbrainz_artist",
                    source="musicbrainz_catalog",
                )
            )
            with (
                patch(
                    "auralis_backend.search.service.load_catalog_artist_payloads",
                    return_value={},
                ),
                patch(
                    "auralis_backend.storage.artist_artwork.get_artist_artwork_cache",
                    return_value=SimpleNamespace(
                        head=Mock(return_value={"content_length": 100})
                    ),
                ),
            ):
                reused = SearchService(
                    memory_server
                )._hydrate_artist_artwork(
                    [
                        {
                            "id": "musicbrainz:artist:mbid-queensryche",
                            "musicbrainz_artist_id": "mbid-queensryche",
                            "name": "Queensrÿche",
                            "artist_aliases": ["Queensryche"],
                        }
                    ],
                    allow_live_lead_lookup=False,
                    schedule_background=False,
                )
            self.assertEqual(
                reused[0].get("provider_artist_id"),
                "UC-Queensryche",
            )
            bridged_record = normalized_artist_payload(reused[0])
            self.assertTrue(
                remember_catalog_entity(
                    memory_server,
                    user_scope_id="global",
                    query="Queensrÿche",
                    entity_type="artist",
                    item=bridged_record,
                    confidence=0.88,
                    event_weight=0.0,
                    event_type="artist_metadata",
                    source="canonical_search_artist",
                )
            )

            connection = open_recommendation_store_connection(memory_server)
            try:
                entity_rows = connection.execute(
                    """
                    SELECT entity_key, payload_json
                    FROM catalog_entities
                    WHERE entity_type = 'artist'
                    """
                ).fetchall()
                alias_entity_keys = {
                    row["entity_key"]
                    for row in connection.execute(
                        """
                        SELECT entity_key
                        FROM catalog_entity_aliases
                        WHERE entity_type = 'artist'
                        """
                    ).fetchall()
                }
            finally:
                connection.close()

            self.assertEqual(len(entity_rows), 1)
            self.assertEqual(
                entity_rows[0]["entity_key"],
                "musicbrainz:artist:mbid-queensryche",
            )
            persisted = json.loads(entity_rows[0]["payload_json"])
            self.assertEqual(
                persisted.get("provider_artist_id"),
                "UC-Queensryche",
            )
            self.assertEqual(
                alias_entity_keys,
                {"musicbrainz:artist:mbid-queensryche"},
            )

            service = SearchService(memory_server)
            with (
                patch(
                    "auralis_backend.search.service.load_catalog_artist_payloads",
                    return_value={},
                ),
                patch(
                    "auralis_backend.search.service._SEARCH_CATALOG_WRITER.submit",
                ),
                patch(
                    "auralis_backend.search.service._schedule_artist_metadata_resolution",
                ) as mock_schedule,
                patch(
                    "auralis_backend.storage.artist_artwork.get_artist_artwork_cache",
                    return_value=SimpleNamespace(
                        head=Mock(return_value={"content_length": 100})
                    ),
                ),
            ):
                related = service._resolve_first_page_related_artists(
                    [
                        {
                            "id": "musicbrainz:artist:mbid-queensryche",
                            "musicbrainz_artist_id": "mbid-queensryche",
                            "name": "Queensrÿche",
                        }
                    ],
                    query="Dio",
                )

            self.assertEqual(
                related[0].get("provider_artist_id"),
                "UC-Queensryche",
            )
            self.assertEqual(related[0].get("thumbnail"), cached_path)
            mock_schedule.assert_not_called()

    def test_related_artist_provider_resolution_is_bounded_per_pass(self) -> None:
        service = SearchService(_MemoryTestServer(":memory:"))
        candidates = [
            {
                "id": f"musicbrainz:artist:mbid-{index}",
                "musicbrainz_artist_id": f"mbid-{index}",
                "name": f"Related Artist {index}",
            }
            for index in range(10)
        ]
        with (
            patch.object(
                service,
                "_hydrate_artist_artwork",
                side_effect=lambda artists, **_kwargs: artists,
            ),
            patch(
                "auralis_backend.search.service._SEARCH_CATALOG_WRITER.submit",
            ),
            patch(
                "auralis_backend.search.service._schedule_artist_metadata_resolution",
                return_value=True,
            ) as mock_schedule,
        ):
            service._resolve_first_page_related_artists(
                candidates,
                query="Dio",
                limit=6,
            )

        self.assertEqual(
            mock_schedule.call_count,
            search_service_module._SEARCH_RELATED_ARTIST_RESOLUTION_BATCH,
        )

    def test_thin_artist_update_cannot_erase_cached_r2_artwork(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(
                str(pathlib.Path(tmp_dir) / "artist-artwork.sqlite")
            )
            cached_path = (
                "/artist_artwork/0123456789abcdef0123456789abcdef"
            )
            self.assertTrue(
                remember_catalog_entity(
                    memory_server,
                    user_scope_id="global",
                    query="Iron Maiden",
                    entity_type="artist",
                    item={
                        "id": "UC-IronMaiden",
                        "name": "Iron Maiden",
                        "thumbnail": cached_path,
                    },
                    confidence=0.88,
                    event_weight=0.0,
                    event_type="artist_metadata",
                    source="test_cached_artwork",
                )
            )
            self.assertTrue(
                remember_catalog_entity(
                    memory_server,
                    user_scope_id="global",
                    query="Iron Maiden",
                    entity_type="artist",
                    item={
                        "id": "UC-IronMaiden",
                        "name": "Iron Maiden",
                        "thumbnail": "",
                    },
                    confidence=0.88,
                    event_weight=0.0,
                    event_type="artist_metadata",
                    source="test_thin_refresh",
                )
            )

            loaded = load_catalog_artist_records(
                memory_server,
                artist_names=["Iron Maiden"],
            )
            self.assertEqual(
                loaded["iron maiden"].get("thumbnail"),
                cached_path,
            )

    def test_live_artist_artwork_replaces_stale_internal_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(
                str(pathlib.Path(tmp_dir) / "artist-artwork-repair.sqlite")
            )
            for thumbnail, source in (
                (
                    "/artist_artwork/0123456789abcdef0123456789abcdef",
                    "test_stale_artwork",
                ),
                ("https://example.test/dio.jpg", "test_live_artwork"),
            ):
                self.assertTrue(
                    remember_catalog_entity(
                        memory_server,
                        user_scope_id="global",
                        query="Dio",
                        entity_type="artist",
                        item={
                            "id": "UC-Dio",
                            "name": "Dio",
                            "thumbnail": thumbnail,
                        },
                        confidence=0.88,
                        event_weight=0.0,
                        event_type="artist_metadata",
                        source=source,
                    )
                )

            loaded = load_catalog_artist_records(
                memory_server,
                artist_names=["Dio"],
            )
            self.assertEqual(
                loaded["dio"].get("thumbnail"),
                "https://example.test/dio.jpg",
            )

    def test_thin_track_and_album_updates_cannot_erase_artwork(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(
                str(pathlib.Path(tmp_dir) / "catalog-artwork.sqlite")
            )
            fixtures = (
                (
                    "track",
                    {
                        "id": "abcdefghijk",
                        "videoId": "abcdefghijk",
                        "title": "The Trooper",
                        "artist": "Iron Maiden",
                        "thumbnail": "https://example.test/trooper.jpg",
                    },
                ),
                (
                    "album",
                    {
                        "id": "MPRE-piece-of-mind",
                        "title": "Piece of Mind",
                        "artist": "Iron Maiden",
                        "thumbnail": "https://example.test/piece-of-mind.jpg",
                    },
                ),
            )
            for entity_type, rich_item in fixtures:
                self.assertTrue(
                    remember_catalog_entity(
                        memory_server,
                        user_scope_id="global",
                        query=rich_item["title"],
                        entity_type=entity_type,
                        item=rich_item,
                        confidence=0.88,
                        event_weight=0.0,
                        event_type="catalog_metadata",
                        source="test_rich",
                    )
                )
                self.assertTrue(
                    remember_catalog_entity(
                        memory_server,
                        user_scope_id="global",
                        query=rich_item["title"],
                        entity_type=entity_type,
                        item={
                            **rich_item,
                            "thumbnail": "",
                        },
                        confidence=0.88,
                        event_weight=0.0,
                        event_type="catalog_metadata",
                        source="test_thin",
                    )
                )

            connection = open_recommendation_store_connection(memory_server)
            try:
                rows = connection.execute(
                    """
                    SELECT entity_type, payload_json
                    FROM catalog_entities
                    WHERE entity_type IN ('track', 'album')
                    """
                ).fetchall()
            finally:
                connection.close()
            payloads = {
                row["entity_type"]: json.loads(row["payload_json"])
                for row in rows
            }
            self.assertEqual(
                payloads["track"].get("thumbnail"),
                "https://example.test/trooper.jpg",
            )
            self.assertEqual(
                payloads["album"].get("thumbnail"),
                "https://example.test/piece-of-mind.jpg",
            )

    def test_snapshot_rehydrates_artwork_cached_after_initial_search(self) -> None:
        service = SearchService(_MemoryTestServer(":memory:"))
        cached_path = artist_artwork_path(
            "provider:artist:uc-arcticmonkeys"
        )
        with (
            patch(
                "auralis_backend.search.service.load_catalog_artist_payloads",
                return_value={},
            ),
            patch(
                "auralis_backend.search.service.load_catalog_artist_records",
                return_value={
                    "arctic monkeys": {
                        "id": "UC-ArcticMonkeys",
                        "name": "Arctic Monkeys",
                        "canonical_artist_id": "artist-name:arctic monkeys",
                        "thumbnail": cached_path,
                    }
                },
            ),
            patch(
                "auralis_backend.storage.artist_artwork.get_artist_artwork_cache",
                return_value=SimpleNamespace(
                    head=Mock(return_value={"content_length": 100})
                ),
            ),
        ):
            refreshed = service._rehydrate_search_snapshot(
                {
                    "artists": [
                        {
                            "id": "UC-ArcticMonkeys",
                            "name": "Arctic Monkeys",
                            "thumbnail": "",
                        },
                        {
                            "id": "UC-ArcticMonkeys",
                            "name": "Arctic Monkeys",
                            "canonical_artist_id": "artist-name:arctic monkeys",
                        },
                    ],
                    "related_artists": [],
                    "lead_artist": {
                        "id": "UC-ArcticMonkeys",
                        "name": "Arctic Monkeys",
                    },
                }
            )

        self.assertEqual(len(refreshed.get("artists") or []), 1)
        self.assertEqual(
            (refreshed.get("artists") or [])[0].get("thumbnail"),
            cached_path,
        )
        self.assertEqual(
            (refreshed.get("lead_artist") or {}).get("thumbnail"),
            cached_path,
        )

    def test_artist_hydration_rejects_same_name_cache_for_another_provider(self) -> None:
        service = SearchService(_MemoryTestServer(":memory:"))
        with (
            patch(
                "auralis_backend.search.service.load_catalog_artist_payloads",
                return_value={},
            ),
            patch(
                "auralis_backend.search.service.load_catalog_artist_records",
                return_value={
                    "nirvana": {
                        "id": "UC-obscure-nirvana",
                        "name": "Nirvana",
                        "thumbnail": "/artist_artwork/wrong",
                    }
                },
            ),
            patch(
                "auralis_backend.search.service._SEARCH_CATALOG_WRITER.submit",
            ),
        ):
            hydrated = service._hydrate_artist_artwork(
                [{"id": "UC-main-nirvana", "name": "Nirvana"}],
                allow_live_lead_lookup=False,
            )

        self.assertEqual(hydrated[0].get("id"), "UC-main-nirvana")
        self.assertFalse(hydrated[0].get("thumbnail"))

    def test_missing_artist_artwork_source_is_resolved_before_r2_cache(self) -> None:
        artist = {
            "id": "UC-ArcticMonkeys",
            "name": "Arctic Monkeys",
            "thumbnail": "",
        }
        with (
            patch.object(
                search_service_module.SearchServerAdapter,
                "build_artist_details_payload",
                return_value={
                    "id": "UC-ArcticMonkeys",
                    "name": "Arctic Monkeys",
                    "thumbnail": "https://example.test/arctic.jpg",
                },
            ) as mock_details,
            patch.object(
                search_service_module,
                "_persist_search_artist",
            ) as mock_persist,
            patch.object(
                search_service_module,
                "schedule_artist_artwork_cache",
                return_value=True,
            ) as mock_cache,
        ):
            search_service_module._resolve_artist_metadata_background(
                server=_MemoryTestServer(":memory:"),
                query="artic monkey",
                artist=artist,
            )

        mock_details.assert_called_once_with(
            "UC-ArcticMonkeys",
            enrich_related=False,
            lightweight=True,
        )
        resolved = mock_persist.call_args.kwargs["artist"]
        self.assertEqual(
            resolved.get("thumbnail"),
            "https://example.test/arctic.jpg",
        )
        self.assertTrue(mock_cache.called)

    def test_malformed_artist_thumbnail_is_replaced_from_artist_detail(self) -> None:
        provider_id = "UCrPe3hLA51968GwxHSZ1llw"
        artist = {
            "id": provider_id,
            "provider_artist_id": provider_id,
            "name": "Nirvana",
            "thumbnail": (
                "https://i.ytimg.com/vi/"
                f"{provider_id}/hqdefault.jpg"
            ),
        }
        valid_source = "https://yt3.googleusercontent.com/nirvana-avatar"
        with (
            patch.object(
                search_service_module.SearchServerAdapter,
                "build_artist_details_payload",
                return_value={
                    "id": provider_id,
                    "name": "Nirvana",
                    "thumbnail": valid_source,
                },
            ) as mock_details,
            patch.object(
                search_service_module,
                "_persist_search_artist",
            ) as mock_persist,
            patch.object(
                search_service_module,
                "schedule_artist_artwork_cache",
                return_value=True,
            ) as mock_cache,
        ):
            search_service_module._resolve_artist_metadata_background(
                server=_MemoryTestServer(":memory:"),
                query="in bloom",
                artist=artist,
            )

        mock_details.assert_called_once()
        resolved = mock_persist.call_args.kwargs["artist"]
        self.assertEqual(resolved.get("thumbnail"), valid_source)
        mock_cache.assert_called_once()

    def test_existing_artist_artwork_source_is_scheduled_for_verified_cache(self) -> None:
        artist = {
            "id": "UC-Nirvana",
            "name": "Nirvana",
            "thumbnail": "https://example.test/nirvana.jpg",
        }
        with (
            patch.object(
                search_service_module,
                "attach_cached_artist_artwork",
                return_value=dict(artist),
            ),
            patch.object(
                search_service_module,
                "schedule_artist_artwork_cache",
                return_value=True,
            ) as mock_cache,
            patch.object(
                search_service_module,
                "_schedule_artist_metadata_resolution",
            ) as mock_metadata,
        ):
            hydrated = search_service_module._ensure_verified_artist_artwork(
                server=_MemoryTestServer(":memory:"),
                query="in bloom",
                artist=artist,
            )

        self.assertEqual(hydrated.get("thumbnail"), artist["thumbnail"])
        mock_cache.assert_called_once()
        mock_metadata.assert_not_called()

    def test_musicbrainz_artist_id_is_resolved_before_artist_detail_lookup(self) -> None:
        artist = {
            "id": "musicbrainz:artist:mbid-nirvana",
            "name": "Nirvana",
            "musicbrainz_artist_id": "mbid-nirvana",
            "thumbnail": "",
        }
        with (
            patch.object(
                search_service_module,
                "search_artists_direct_cached",
                return_value=[
                    {
                        "id": "UC-Nirvana",
                        "name": "Nirvana",
                        "thumbnail": "https://example.test/nirvana-search.jpg",
                    }
                ],
            ) as mock_artist_search,
            patch.object(
                search_service_module.SearchServerAdapter,
                "build_artist_details_payload",
                return_value={
                    "id": "UC-Nirvana",
                    "name": "Nirvana",
                    "thumbnail": "https://example.test/nirvana-detail.jpg",
                },
            ) as mock_details,
            patch.object(
                search_service_module,
                "_persist_search_artist",
            ) as mock_persist,
            patch.object(
                search_service_module,
                "schedule_artist_artwork_cache",
                return_value=True,
            ),
        ):
            search_service_module._resolve_artist_metadata_background(
                server=_MemoryTestServer(":memory:"),
                query="nirvana",
                artist=artist,
            )

        mock_artist_search.assert_called_once()
        mock_details.assert_called_once_with(
            "UC-Nirvana",
            enrich_related=False,
            lightweight=True,
        )
        resolved = mock_persist.call_args.kwargs["artist"]
        self.assertEqual(resolved.get("provider_artist_id"), "UC-Nirvana")
        self.assertEqual(
            resolved.get("musicbrainz_artist_id"),
            "mbid-nirvana",
        )

    def test_lastfm_related_artists_preserve_distinct_canonical_homonyms(self) -> None:
        client = Mock()
        client.similar_artists.return_value = [
            {
                "id": "musicbrainz:artist:foo",
                "name": "Foo",
                "musicbrainz_artist_id": "foo",
                "relationship_provider": "lastfm",
            },
            {
                "id": "musicbrainz:artist:foo-copy",
                "name": "FOO",
                "relationship_provider": "lastfm",
            },
            {
                "id": "musicbrainz:artist:lead",
                "name": "Lead Artist",
                "relationship_provider": "lastfm",
            },
        ]
        with patch.object(
            search_service_module,
            "LastFmClient",
            return_value=client,
        ):
            related = SearchService(
                _MemoryTestServer(":memory:")
            )._lastfm_related_artists(
                {
                    "name": "Lead Artist",
                    "musicbrainz_artist_id": "lead",
                },
                limit=16,
            )

        self.assertEqual([item.get("name") for item in related], ["Foo", "FOO"])
        self.assertEqual(
            related[0].get("relationship_provider"),
            "lastfm",
        )
        client.similar_artists.assert_called_once_with(
            "Lead Artist",
            artist_mbid="lead",
            limit=16,
        )

    def test_artist_artwork_path_is_stable_per_canonical_artist(self) -> None:
        first = artist_artwork_token("artist-name:queen")
        second = artist_artwork_token("ARTIST-NAME:QUEEN")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)
        self.assertEqual(
            artist_artwork_path("artist-name:queen"),
            f"/artist_artwork/{first}",
        )

    def test_artist_artwork_rejects_channel_id_video_thumbnail(self) -> None:
        valid_source = "https://yt3.googleusercontent.com/nirvana-avatar"
        artist = normalized_artist_payload(
            {
                "id": "UCrPe3hLA51968GwxHSZ1llw",
                "name": "Nirvana",
                "thumbnail": (
                    "https://i.ytimg.com/vi/"
                    "UCrPe3hLA51968GwxHSZ1llw/hqdefault.jpg"
                ),
                "artwork_source_urls": [valid_source],
            }
        )

        self.assertEqual(artist.get("thumbnail"), valid_source)
        self.assertEqual(artist.get("artwork_source_urls"), [valid_source])

    def test_canonical_artist_reuses_valid_provider_artwork_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(
                str(pathlib.Path(tmp_dir) / "artist-artwork-repair.sqlite")
            )
            provider_id = "UCrPe3hLA51968GwxHSZ1llw"
            valid_source = "https://yt3.googleusercontent.com/nirvana-avatar"
            self.assertTrue(
                remember_catalog_entity(
                    memory_server,
                    user_scope_id="global",
                    query="Nirvana",
                    entity_type="artist",
                    item={
                        "id": provider_id,
                        "provider_artist_id": provider_id,
                        "name": "Nirvana",
                        "thumbnail": valid_source,
                    },
                    confidence=0.88,
                    event_weight=0.0,
                    event_type="artist_metadata",
                    source="provider_artist_detail",
                )
            )
            self.assertTrue(
                remember_catalog_entity(
                    memory_server,
                    user_scope_id="global",
                    query="Nirvana",
                    entity_type="artist",
                    item={
                        "id": "musicbrainz:artist:mbid-nirvana",
                        "provider_artist_id": provider_id,
                        "musicbrainz_artist_id": "mbid-nirvana",
                        "canonical_artist_id": (
                            "musicbrainz:artist:mbid-nirvana"
                        ),
                        "name": "Nirvana",
                        "thumbnail": (
                            "https://i.ytimg.com/vi/"
                            f"{provider_id}/hqdefault.jpg"
                        ),
                    },
                    confidence=0.92,
                    event_weight=0.0,
                    event_type="artist_metadata",
                    source="musicbrainz_artist",
                )
            )

            loaded = load_catalog_artist_records(
                memory_server,
                artist_names=["Nirvana"],
            )["nirvana"]

        self.assertEqual(loaded.get("thumbnail"), valid_source)
        self.assertEqual(loaded.get("artwork_source_urls"), [valid_source])

    def test_artist_artwork_cache_tries_next_valid_source(self) -> None:
        first_source = "https://yt3.googleusercontent.com/unavailable-avatar"
        second_source = "https://example.test/verified-avatar.jpg"
        cache = SimpleNamespace(
            head=Mock(return_value=None),
            store=Mock(side_effect=[False, True]),
        )
        completed = []

        def run_immediately(callback):
            callback()
            future = Future()
            future.set_result(None)
            return future

        with (
            patch(
                "auralis_backend.storage.artist_artwork.get_artist_artwork_cache",
                return_value=cache,
            ),
            patch(
                "auralis_backend.storage.artist_artwork._EXECUTOR.submit",
                side_effect=run_immediately,
            ),
        ):
            scheduled = schedule_artist_artwork_cache(
                object(),
                {
                    "id": "UC-Nirvana",
                    "name": "Nirvana",
                    "artwork_source_urls": [first_source, second_source],
                },
                on_cached=completed.append,
            )

        self.assertTrue(scheduled)
        self.assertEqual(cache.store.call_count, 2)
        self.assertEqual(completed[0].get("artwork_source_url"), second_source)
        self.assertEqual(
            completed[0].get("artwork_failed_source_urls"),
            [first_source],
        )

    def test_existing_provider_artwork_object_is_reused_without_source_url(
        self,
    ) -> None:
        cache = SimpleNamespace(
            head=Mock(
                side_effect=lambda token: (
                    {"content_length": 100}
                    if token
                    == artist_artwork_token("provider:artist:uc-ironmaiden")
                    else None
                )
            )
        )
        with patch(
            "auralis_backend.storage.artist_artwork.get_artist_artwork_cache",
            return_value=cache,
        ):
            artist = attach_cached_artist_artwork(
                object(),
                {
                    "id": "UC-IronMaiden",
                    "name": "Iron Maiden",
                    "canonical_artist_id": "artist-name:iron maiden",
                },
            )
        self.assertTrue(str(artist.get("thumbnail")).startswith("/artist_artwork/"))
        self.assertEqual(
            artist.get("artwork_cache_identity"),
            "provider:artist:uc-ironmaiden",
        )

    def test_provider_artist_rejects_legacy_same_name_artwork_object(self) -> None:
        legacy_name_token = artist_artwork_token("artist-name:in bloom")
        cache = SimpleNamespace(
            head=Mock(
                side_effect=lambda token: (
                    {"content_length": 100}
                    if token == legacy_name_token
                    else None
                )
            )
        )
        with patch(
            "auralis_backend.storage.artist_artwork.get_artist_artwork_cache",
            return_value=cache,
        ):
            artist = attach_cached_artist_artwork(
                object(),
                {
                    "id": "UC-InBloom",
                    "name": "In Bloom",
                    "thumbnail": f"/artist_artwork/{legacy_name_token}",
                    "artwork_cache_token": legacy_name_token,
                },
            )

        self.assertFalse(artist.get("thumbnail"))
        self.assertNotEqual(artist.get("artwork_cache_token"), legacy_name_token)

    def test_stale_artist_artwork_path_falls_back_to_source_url(self) -> None:
        with patch(
            "auralis_backend.storage.artist_artwork.get_artist_artwork_cache",
            return_value=SimpleNamespace(head=Mock(return_value=None)),
        ):
            artist = attach_cached_artist_artwork(
                object(),
                {
                    "id": "UC-Dio",
                    "name": "Dio",
                    "thumbnail": (
                        "/artist_artwork/"
                        "0123456789abcdef0123456789abcdef"
                    ),
                    "artwork_source_url": "https://example.test/dio.jpg",
                },
            )

        self.assertEqual(
            artist.get("thumbnail"),
            "https://example.test/dio.jpg",
        )

    def test_stale_artist_artwork_path_without_source_is_removed(self) -> None:
        with patch(
            "auralis_backend.storage.artist_artwork.get_artist_artwork_cache",
            return_value=SimpleNamespace(head=Mock(return_value=None)),
        ):
            artist = attach_cached_artist_artwork(
                object(),
                {
                    "id": "UC-Dio",
                    "name": "Dio",
                    "thumbnail": (
                        "/artist_artwork/"
                        "0123456789abcdef0123456789abcdef"
                    ),
                },
            )

        self.assertFalse(artist.get("thumbnail"))

    def test_related_artist_visibility_requires_usable_artwork(self) -> None:
        service = SearchService(_MemoryTestServer(":memory:"))
        valid_token = artist_artwork_token("provider:artist:uc-valid")
        with patch(
            "auralis_backend.storage.artist_artwork.get_artist_artwork_cache",
            return_value=SimpleNamespace(
                head=Mock(
                    side_effect=lambda token: (
                        {"content_length": 100}
                        if token == valid_token
                        else None
                    )
                )
            ),
        ):
            visible = service._visible_artists(
                [
                    {
                        "id": "UC-dead",
                        "name": "Dead Artwork",
                        "thumbnail": (
                            "/artist_artwork/"
                            "0123456789abcdef0123456789abcdef"
                        ),
                    },
                    {
                        "id": "UC-valid",
                        "name": "Valid Artwork",
                        "thumbnail": "https://example.test/valid.jpg",
                    },
                    {
                        "id": "UC-missing",
                        "name": "Missing Artwork",
                    },
                    {
                        "id": "musicbrainz:artist:unresolved",
                        "name": "Unresolved Artist",
                        "thumbnail": "https://example.test/unresolved.jpg",
                    },
                ],
                {"id": "UC-Dio", "name": "Dio"},
            )

        self.assertEqual(
            [artist.get("name") for artist in visible],
            ["Valid Artwork"],
        )
        self.assertEqual(
            visible[0].get("thumbnail"),
            f"/artist_artwork/{valid_token}",
        )

    def test_background_artist_completion_advances_snapshot_revision(self) -> None:
        service = SearchService(_MemoryTestServer(":memory:"))
        snapshot_key = "progressive-test||dio"
        search_service_module._store_search_snapshot(
            snapshot_key,
            {
                "revision": 1,
                "lead_artist": {"id": "UC-Dio", "name": "Dio"},
                "artists": [{"id": "UC-Dio", "name": "Dio"}],
                "related_artists": [],
                "expansion_state": {"artists": "retryable"},
            },
        )

        def complete(*, snapshot, **_kwargs):
            snapshot["related_artists"] = [
                {
                    "id": "UC-Elf",
                    "name": "Elf",
                    "thumbnail": "https://example.test/elf.jpg",
                }
            ]
            snapshot["expansion_state"] = {"artists": "complete"}
            return snapshot

        with (
            patch.object(
                service,
                "_expand_search_snapshot_surface",
                side_effect=complete,
            ),
            patch.object(
                search_service_module._SEARCH_ARTIST_METADATA_WRITER,
                "submit",
                side_effect=lambda function, *args, **kwargs: function(
                    *args,
                    **kwargs,
                ),
            ),
        ):
            self.assertTrue(
                service._schedule_search_snapshot_completion(
                    snapshot_key=snapshot_key,
                    query="Dio",
                    search_mode="exact",
                    user_scope_id="progressive-test",
                )
            )

        refreshed = search_service_module._load_search_snapshot(snapshot_key)
        self.assertEqual(refreshed.get("revision"), 2)
        self.assertEqual(
            [artist.get("name") for artist in refreshed["related_artists"]],
            ["Elf"],
        )

    def test_background_identity_recovery_keeps_catalog_retryable(self) -> None:
        service = SearchService(_MemoryTestServer(":memory:"))
        recovered_target = {
            "entity_type": "track",
            "confidence_tier": "authoritative",
            "target_identity": "musicbrainz:recording:mb-everlong",
            "lead_artist": {
                "id": "UC-FooFighters",
                "name": "Foo Fighters",
                "thumbnail": "https://example.test/foo.jpg",
            },
            "containing_album": {
                "id": "MPRE-ColourAndShape",
                "title": "The Colour and the Shape",
                "artist": "Foo Fighters",
                "thumbnail": "https://example.test/colour.jpg",
            },
        }
        snapshot = {
            "query_intent": "mixed",
            "resolved_target": {},
            "lead_artist": {},
            "tracks": [],
            "artists": [],
            "albums": [],
            "artist_tracks": [],
            "artist_albums": [],
            "related_artists": [],
            "related_albums": [],
            "expansion_state": {
                "tracks": "retryable",
                "artists": "retryable",
                "albums": "retryable",
            },
        }

        with (
            patch(
                "auralis_backend.search.service.retrieve_search_candidates_fast",
                return_value={
                    "resolved_target": recovered_target,
                    "related_artists": [],
                },
            ),
            patch(
                "auralis_backend.search.service.rank_artist_candidates_fast_path",
                return_value=[recovered_target["lead_artist"]],
            ),
            patch(
                "auralis_backend.search.service.rank_track_candidates_fast_path",
                return_value=[],
            ),
            patch(
                "auralis_backend.search.service.rank_album_candidates_fast_path",
                return_value=[],
            ),
            patch.object(
                service,
                "_hydrate_artist_artwork",
                side_effect=lambda items, **_kwargs: list(items),
            ),
            patch.object(service, "_lastfm_related_artists", return_value=[]),
        ):
            refreshed = service._expand_search_snapshot_surface(
                req=SimpleNamespace(user_scope_id="user-1"),
                query="Everlong",
                search_mode="exact",
                surface="artists",
                snapshot=snapshot,
            )

        self.assertEqual(
            (refreshed.get("lead_artist") or {}).get("name"),
            "Foo Fighters",
        )
        self.assertEqual(
            (refreshed.get("containing_album") or {}).get("title"),
            "The Colour and the Shape",
        )
        self.assertEqual(
            refreshed.get("expansion_state"),
            {
                "tracks": "retryable",
                "artists": "retryable",
                "albums": "retryable",
            },
        )

    def test_target_essentials_resolve_artist_and_album_in_one_window(self) -> None:
        class InlineExecutor:
            def submit(self, function, *args, **kwargs):
                future = Future()
                try:
                    future.set_result(function(*args, **kwargs))
                except Exception as exc:
                    future.set_exception(exc)
                return future

        test_server = _MemoryTestServer(":memory:")
        test_server.search_executor = InlineExecutor()
        service = SearchService(test_server)
        target = {
            "entity_type": "track",
            "item": {
                "id": "hail-video",
                "title": "Hail to the King",
                "channel": "Avenged Sevenfold",
                "album": "Hail to the King",
            },
            "lead_artist": {"name": "Avenged Sevenfold"},
            "containing_album": {
                "title": "Hail to the King",
                "artist": "Avenged Sevenfold",
            },
        }
        with (
            patch(
                "auralis_backend.search.service.search_artists_direct_cached",
                return_value=[
                    {
                        "id": "UC-A7X",
                        "name": "Avenged Sevenfold",
                        "thumbnail": "https://example.test/a7x.jpg",
                        "source_authority": "official_artist_channel",
                    }
                ],
            ),
            patch(
                "auralis_backend.search.service.search_canonical_album_for_track",
                return_value={
                    "id": "MPRE-Hail",
                    "provider_album_id": "MPRE-Hail",
                    "title": "Hail to the King",
                    "artist": "Avenged Sevenfold",
                    "thumbnail": "https://example.test/hail.jpg",
                },
            ),
        ):
            hydrated = service._hydrate_accepted_target_essentials(target)

        self.assertEqual(
            (hydrated.get("lead_artist") or {}).get("id"),
            "UC-A7X",
        )
        self.assertEqual(
            (hydrated.get("containing_album") or {}).get("id"),
            "MPRE-Hail",
        )

    def test_background_revalidation_replaces_wrong_cross_type_target(self) -> None:
        service = SearchService(_MemoryTestServer(":memory:"))
        corrected_target = {
            "entity_type": "track",
            "confidence_tier": "corroborated",
            "identity_confidence": 0.99,
            "confidence": 0.96,
            "target_identity": "musicbrainz:recording:mb-hail",
            "evidence": [
                "provider_structural_lead",
                "containing_album_relationship",
            ],
            "item": {
                "id": "hail-video",
                "title": "Hail to the King",
                "channel": "Avenged Sevenfold",
            },
            "lead_artist": {"id": "UC-A7X", "name": "Avenged Sevenfold"},
            "containing_album": {
                "id": "MPRE-Hail",
                "title": "Hail to the King",
                "artist": "Avenged Sevenfold",
            },
        }
        snapshot = {
            "query_intent": "artist",
            "resolved_target": {
                "entity_type": "artist",
                "confidence_tier": "authoritative",
                "confidence": 0.88,
                "target_identity": "provider:artist:wrong-hail",
                "item": {"id": "wrong-hail", "name": "Hail to the King"},
                "lead_artist": {
                    "id": "wrong-hail",
                    "name": "Hail to the King",
                },
            },
            "lead_artist": {"id": "wrong-hail", "name": "Hail to the King"},
            "tracks": [corrected_target["item"]],
            "artists": [{"id": "wrong-hail", "name": "Hail to the King"}],
            "albums": [],
            "artist_tracks": [{"id": "wrong-track", "channel": "Wrong"}],
            "artist_albums": [{"id": "MPRE-Wrong", "artist": "Wrong"}],
            "related_artists": [{"id": "wrong-related", "name": "Wrong"}],
            "related_albums": [],
            "playlists": [],
            "expansion_state": {"artists": "retryable"},
            "target_revalidation_state": "complete",
        }
        self.assertTrue(
            service._snapshot_target_needs_revalidation(
                "Hail to the King",
                snapshot,
            )
        )
        with (
            patch(
                "auralis_backend.search.service.retrieve_search_candidates_fast",
                return_value={
                    "resolved_target": corrected_target,
                    "related_artists": [],
                },
            ),
            patch(
                "auralis_backend.search.service.rank_artist_candidates_fast_path",
                return_value=[corrected_target["lead_artist"]],
            ),
            patch(
                "auralis_backend.search.service.rank_track_candidates_fast_path",
                return_value=[corrected_target["item"]],
            ),
            patch(
                "auralis_backend.search.service.rank_album_candidates_fast_path",
                return_value=[],
            ),
            patch.object(
                service,
                "_hydrate_artist_artwork",
                side_effect=lambda items, **_kwargs: list(items),
            ),
            patch.object(service, "_lastfm_related_artists", return_value=[]),
        ):
            refreshed = service._expand_search_snapshot_surface(
                req=SimpleNamespace(user_scope_id="user-1"),
                query="Hail to the King",
                search_mode="exact",
                surface="artists",
                snapshot=snapshot,
                revalidate_target=True,
            )

        self.assertEqual(refreshed.get("query_intent"), "track")
        self.assertEqual(
            (refreshed.get("lead_artist") or {}).get("name"),
            "Avenged Sevenfold",
        )
        self.assertNotIn(
            "wrong-track",
            [item.get("id") for item in refreshed.get("artist_tracks") or []],
        )
        self.assertEqual(refreshed.get("target_revalidation_state"), "complete")
        self.assertEqual(refreshed.get("target_revalidation_attempts"), 1)

    def test_artist_retry_metadata_does_not_advance_visible_revision(self) -> None:
        snapshot_key = "progressive-test||nirvana-retry"
        search_service_module._store_search_snapshot(
            snapshot_key,
            {
                "revision": 4,
                "lead_artist": {"id": "UC-Nirvana", "name": "Nirvana"},
                "artists": [{"id": "UC-Nirvana", "name": "Nirvana"}],
                "related_artists": [],
            },
        )

        search_service_module._record_artist_resolution_attempt(
            {"id": "UC-Nirvana", "name": "Nirvana"}
        )

        refreshed = search_service_module._load_search_snapshot(snapshot_key)
        self.assertEqual(refreshed.get("revision"), 4)

    @patch(
        "auralis_backend.search.runtime.resolve_ytmusic_song_search",
        side_effect=AssertionError("typeahead must not run full song search"),
    )
    @patch(
        "auralis_backend.search.runtime.resolve_search_artists_direct",
        side_effect=AssertionError("typeahead must not run full artist search"),
    )
    @patch(
        "auralis_backend.search.runtime.load_fuzzy_catalog_entity_memories",
        return_value=[],
    )
    @patch("auralis_backend.search.runtime.lookup_search_result", return_value=None)
    def test_typeahead_uses_suggestions_not_full_entity_searches(
        self,
        _mock_cache,
        _mock_catalog,
        mock_artist_search,
        mock_song_search,
    ) -> None:
        req = SimpleNamespace(
            query="unique maiden pref",
            limit=5,
            recent_tracks=[],
            recent_track_snapshots=[],
            recent_queries=[],
            taste_queries=[],
        )
        with patch.object(
            server.ytmusic,
            "get_search_suggestions",
            return_value=["iron maiden"],
        ):
            suggestions = semantic_search_suggestion_items(req, server=server)
        self.assertEqual(suggestions[0].get("text"), "iron maiden")
        mock_artist_search.assert_not_called()
        mock_song_search.assert_not_called()

    def test_suggestions_promote_recent_playable_track_rows(self) -> None:
        class InlineExecutor:
            def submit(self, fn, *args, **kwargs):
                future = Future()
                try:
                    future.set_result(fn(*args, **kwargs))
                except Exception as exc:
                    future.set_exception(exc)
                return future

        class FakeSuggestionServer(_MemoryTestServer):
            class FakeYtMusic:
                def get_search_suggestions(self, query: str):
                    return ["creep radiohead", "creep remix"]

            def __init__(self, db_path: str) -> None:
                super().__init__(db_path)
                self.ytmusic = self.FakeYtMusic()
                self.search_executor = InlineExecutor()
                self.search_upstream_executor = InlineExecutor()
                self.recommendation_executor = self.search_executor

            def _assistant_safe_scope_id(self, value):
                return str(value or "guest")

            def _recommendation_trim_text(self, value):
                return str(value or "").strip()

            def _recommendation_unique_strings(self, values, limit=None):
                result = []
                for value in values or []:
                    text = str(value or "").strip()
                    if text and text not in result:
                        result.append(text)
                    if limit and len(result) >= limit:
                        break
                return result

        with tempfile.TemporaryDirectory() as tmp_dir:
            suggestion_server = FakeSuggestionServer(
                str(pathlib.Path(tmp_dir) / "suggestions.sqlite")
            )
            req = SimpleNamespace(
                query="creep",
                limit=5,
                user_scope_id="user-1",
                recent_queries=[],
                taste_queries=[],
                last_played_tracks=[
                    {
                        "id": "radiohead-creep",
                        "title": "Creep",
                        "artist": "Radiohead",
                        "thumbnail": "thumb",
                    }
                ],
                recent_tracks=[],
                recent_track_snapshots=[],
            )

            suggestions = semantic_search_suggestion_items(req, server=suggestion_server)

            self.assertTrue(suggestions)
            self.assertEqual(suggestions[0].get("suggestion_type"), "track_play")
            self.assertTrue(suggestions[0].get("direct_play"))
            self.assertEqual(suggestions[0].get("track", {}).get("id"), "radiohead-creep")

    def test_direct_play_suggestion_returns_without_upstream_wait(self) -> None:
        class FailingExecutor:
            def submit(self, *_args, **_kwargs):
                raise AssertionError("direct-play suggestion should return before upstream submit")

        class FastLocalSuggestionServer(_MemoryTestServer):
            def __init__(self, db_path: str) -> None:
                super().__init__(db_path)
                self.search_executor = FailingExecutor()
                self.search_upstream_executor = FailingExecutor()
                self.recommendation_executor = self.search_executor

            def _assistant_safe_scope_id(self, value):
                return str(value or "guest")

            def _recommendation_trim_text(self, value):
                return str(value or "").strip()

            def _recommendation_unique_strings(self, values, limit=None):
                result = []
                for value in values or []:
                    text = str(value or "").strip()
                    if text and text not in result:
                        result.append(text)
                    if limit and len(result) >= limit:
                        break
                return result

        with tempfile.TemporaryDirectory() as tmp_dir:
            suggestion_server = FastLocalSuggestionServer(
                str(pathlib.Path(tmp_dir) / "suggestions-fast.sqlite")
            )
            req = SimpleNamespace(
                query="creep",
                limit=5,
                user_scope_id="user-1",
                recent_queries=[],
                taste_queries=[],
                last_played_tracks=[
                    {
                        "id": "radiohead-creep",
                        "title": "Creep",
                        "artist": "Radiohead",
                        "thumbnail": "thumb",
                    }
                ],
                recent_tracks=[],
                recent_track_snapshots=[],
                top_track_snapshots=[],
            )

            started_at = time.perf_counter()
            suggestions = semantic_search_suggestion_items(req, server=suggestion_server)

            self.assertLess(time.perf_counter() - started_at, 0.05)
            self.assertEqual(suggestions[0].get("suggestion_type"), "track_play")
            self.assertEqual(suggestions[0].get("track", {}).get("id"), "radiohead-creep")

    def test_musicbrainz_recording_maps_to_verified_catalog_item(self) -> None:
        item = musicbrainz_recording_to_item(
            {
                "id": "mb-rec-evanescence",
                "title": "Bring Me to Life",
                "score": "100",
                "artist-credit": [
                    {"name": "Evanescence", "artist": {"id": "mb-artist-evanescence"}}
                ],
                "releases": [
                    {
                        "id": "mb-release-fallen",
                        "title": "Fallen",
                        "date": "2003-03-04",
                        "country": "US",
                        "release-group": {"id": "mb-rg-fallen"},
                    }
                ],
            },
            query="bring me life",
        )

        self.assertEqual(item.get("source_provider"), "musicbrainz")
        self.assertEqual(item.get("source_authority"), "verified_catalog")
        self.assertEqual(item.get("musicbrainz_recording_id"), "mb-rec-evanescence")
        self.assertEqual(item.get("album"), "Fallen")
        self.assertIn("Evanescence Bring Me to Life", item.get("aliases") or [])

    def test_recording_prefers_original_album_over_single_and_compilation(self) -> None:
        item = musicbrainz_recording_to_item(
            {
                "id": "mb-rec-in-bloom",
                "title": "In Bloom",
                "score": "100",
                "artist-credit": [
                    {"name": "Nirvana", "artist": {"id": "mb-artist-nirvana"}}
                ],
                "releases": [
                    {
                        "id": "mb-release-compilation",
                        "title": "Greatest Hits",
                        "date": "2002-10-29",
                        "status": "Official",
                        "release-group": {
                            "id": "mb-rg-compilation",
                            "primary-type": "Album",
                            "secondary-types": ["Compilation"],
                        },
                    },
                    {
                        "id": "mb-release-single",
                        "title": "In Bloom",
                        "date": "1992-11-30",
                        "status": "Official",
                        "release-group": {
                            "id": "mb-rg-single",
                            "primary-type": "Single",
                        },
                    },
                    {
                        "id": "mb-release-nevermind",
                        "title": "Nevermind",
                        "date": "1991-09-24",
                        "status": "Official",
                        "release-group": {
                            "id": "mb-rg-nevermind",
                            "primary-type": "Album",
                        },
                    },
                ],
            },
            query="In Bloom",
        )

        self.assertEqual(item.get("album"), "Nevermind")
        self.assertEqual(
            item.get("musicbrainz_release_group_id"),
            "mb-rg-nevermind",
        )
        self.assertEqual(
            len(item.get("musicbrainz_release_candidates") or []),
            3,
        )

    def test_recording_lookup_constrains_title_artist_and_release_kind(self) -> None:
        class RecordingClient:
            def __init__(self) -> None:
                self.query = ""
                self.limit = 0

            def search_recordings(self, query, *, limit):
                self.query = query
                self.limit = limit
                return []

        client = RecordingClient()
        results = search_musicbrainz_recording_items(
            "The Trooper",
            artist="Iron Maiden",
            official_non_live=True,
            raise_errors=True,
            client=client,
            limit=25,
        )

        self.assertEqual(results, [])
        self.assertEqual(client.limit, 25)
        self.assertIn('recording:"The Trooper"', client.query)
        self.assertIn('artistname:"Iron Maiden"', client.query)
        self.assertIn("status:official", client.query)
        self.assertIn("-secondarytype:live", client.query)
        self.assertIn("video:false", client.query)

    def test_recording_lookup_can_report_provider_failure(self) -> None:
        class FailingRecordingClient:
            def search_recordings(self, _query, *, limit):
                del limit
                raise TimeoutError("musicbrainz timed out")

        with self.assertRaises(TimeoutError):
            search_musicbrainz_recording_items(
                "In Bloom",
                artist="Nirvana",
                raise_errors=True,
                client=FailingRecordingClient(),
            )

    def test_musicbrainz_artist_and_release_group_map_to_verified_catalog_items(self) -> None:
        artist = musicbrainz_artist_to_item(
            {
                "id": "mb-artist-prince",
                "name": "Prince",
                "score": "100",
                "country": "US",
            },
            query="purple rain",
        )
        album = musicbrainz_release_group_to_item(
            {
                "id": "mb-rg-purple-rain",
                "title": "Purple Rain",
                "score": "100",
                "first-release-date": "1984-06-25",
                "artist-credit": [
                    {"name": "Prince and The Revolution", "artist": {"id": "mb-artist-prince"}}
                ],
            },
            query="purple rain",
        )

        self.assertEqual(artist.get("source_authority"), "verified_catalog")
        self.assertEqual(artist.get("musicbrainz_artist_id"), "mb-artist-prince")
        self.assertEqual(album.get("musicbrainz_release_group_id"), "mb-rg-purple-rain")
        self.assertEqual(album.get("release_year"), "1984")

    def test_musicbrainz_enrichment_imports_alias_memory(self) -> None:
        class FakeMusicBrainzClient:
            def search_recordings(self, query: str, *, limit: int = 5):
                return [
                    {
                        "id": "mb-rec-november-rain",
                        "title": "November Rain",
                        "score": "100",
                        "artist-credit": [
                            {
                                "name": "Guns N' Roses",
                                "artist": {"id": "mb-artist-gnr"},
                            }
                        ],
                        "releases": [
                            {
                                "id": "mb-release-uyi",
                                "title": "Use Your Illusion I",
                                "date": "1991-09-17",
                                "release-group": {"id": "mb-rg-uyi"},
                            }
                        ],
                    }
                ]

            def search_artists(self, query: str, *, limit: int = 5):
                return [
                    {
                        "id": "mb-artist-gnr",
                        "name": "Guns N' Roses",
                        "score": "100",
                    }
                ]

            def search_release_groups(self, query: str, *, limit: int = 5):
                return [
                    {
                        "id": "mb-rg-uyi",
                        "title": "Use Your Illusion I",
                        "score": "100",
                        "first-release-date": "1991-09-17",
                        "artist-credit": [
                            {
                                "name": "Guns N' Roses",
                                "artist": {"id": "mb-artist-gnr"},
                            }
                        ],
                    }
                ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "musicbrainz.sqlite"))

            result = enrich_query_with_musicbrainz(
                memory_server,
                user_scope_id="user-1",
                query="gnr november rain",
                client=FakeMusicBrainzClient(),
            )
            memories = load_catalog_entity_memories(
                memory_server,
                query="guns roses november rain",
                entity_type="track",
            )

            self.assertEqual(result.get("imported_tracks"), 1)
            self.assertEqual(result.get("imported_artists"), 1)
            self.assertEqual(result.get("imported_albums"), 1)
            self.assertTrue(memories)
            self.assertEqual(memories[0].get("artist_key"), "guns n roses")
            self.assertTrue(
                load_catalog_entity_memories(
                    memory_server,
                    query="guns n roses",
                    entity_type="artist",
                )
            )
            self.assertEqual(
                load_catalog_artist_records(
                    memory_server,
                    artist_names=["Guns N' Roses"],
                )["guns n roses"].get("musicbrainz_artist_id"),
                "mb-artist-gnr",
            )
            self.assertTrue(
                load_catalog_entity_memories(
                    memory_server,
                    query="use your illusion i",
                    entity_type="album",
                )
            )

    def test_external_catalog_import_queue_dedupes_and_imports_musicbrainz(self) -> None:
        class FakeMusicBrainzClient:
            def search_recordings(self, query: str, *, limit: int = 5):
                return [
                    {
                        "id": "mb-rec-creep",
                        "title": "Creep",
                        "score": "100",
                        "artist-credit": [
                            {"name": "Radiohead", "artist": {"id": "mb-artist-radiohead"}}
                        ],
                        "releases": [
                            {
                                "id": "mb-release-pablo",
                                "title": "Pablo Honey",
                                "date": "1993-02-22",
                                "release-group": {"id": "mb-rg-pablo"},
                            }
                        ],
                    }
                ]

            def search_artists(self, query: str, *, limit: int = 5):
                return [
                    {
                        "id": "mb-artist-radiohead",
                        "name": "Radiohead",
                        "score": "100",
                    }
                ]

            def search_release_groups(self, query: str, *, limit: int = 5):
                return [
                    {
                        "id": "mb-rg-pablo",
                        "title": "Pablo Honey",
                        "score": "100",
                        "first-release-date": "1993-02-22",
                        "artist-credit": [
                            {"name": "Radiohead", "artist": {"id": "mb-artist-radiohead"}}
                        ],
                    }
                ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "external-import.sqlite"))

            queued = enqueue_external_catalog_seeds(
                memory_server,
                [
                    {"query": "creep radiohead", "seed_type": "test", "priority": 1.0},
                    {"query": "Creep Radiohead", "seed_type": "test", "priority": 0.2},
                ],
                user_scope_id="user-1",
                provider="musicbrainz",
                source="test",
            )
            imported = run_external_catalog_import(
                memory_server,
                user_scope_id="user-1",
                provider="musicbrainz",
                batch_size=4,
                musicbrainz_client=FakeMusicBrainzClient(),
            )
            memories = load_catalog_entity_memories(
                memory_server,
                query="radiohead creep",
                entity_type="track",
            )

            self.assertEqual(queued.get("queued"), 1)
            self.assertEqual(imported.get("processed"), 1)
            self.assertEqual(imported.get("completed"), 1)
            self.assertGreaterEqual(imported.get("imported"), 3)
            self.assertTrue(memories)
            self.assertEqual(memories[0].get("artist_key"), "radiohead")

            progress = external_catalog_import_progress(memory_server)
            self.assertEqual(progress.get("queue_by_status", {}).get("completed"), 1)
            self.assertGreaterEqual(progress.get("catalog_total") or 0, 3)
            self.assertGreater(progress.get("alias_total") or 0, 0)

            coverage = catalog_import_coverage_report(
                memory_server,
                fixtures=[
                    {
                        "query": "creep radiohead",
                        "expected_title": "Creep",
                        "expected_artist": "Radiohead",
                    }
                ],
            )
            self.assertEqual(coverage.get("fixture_total"), 1)
            self.assertEqual(coverage.get("fixture_passed"), 1)
            self.assertFalse(coverage.get("production_usable"))
            self.assertEqual(0.0, coverage.get("track_playable_source_ratio"))

    def test_external_catalog_import_marks_no_result_seed(self) -> None:
        class EmptyMusicBrainzClient:
            def search_recordings(self, query: str, *, limit: int = 5):
                return []

            def search_artists(self, query: str, *, limit: int = 5):
                return []

            def search_release_groups(self, query: str, *, limit: int = 5):
                return []

        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "external-empty.sqlite"))
            enqueue_external_catalog_seeds(
                memory_server,
                ["not a real catalog seed"],
                user_scope_id="user-1",
                provider="musicbrainz",
                source="test",
            )

            first = run_external_catalog_import(
                memory_server,
                user_scope_id="user-1",
                provider="musicbrainz",
                batch_size=4,
                musicbrainz_client=EmptyMusicBrainzClient(),
            )
            second = run_external_catalog_import(
                memory_server,
                user_scope_id="user-1",
                provider="musicbrainz",
                batch_size=4,
                musicbrainz_client=EmptyMusicBrainzClient(),
            )

            self.assertEqual(first.get("processed"), 1)
            self.assertEqual(first.get("no_results"), 1)
            self.assertEqual(second.get("processed"), 0)


    def test_bare_official_label_is_not_source_authority(self) -> None:
        score = source_quality_score(
            SearchService(server)._search_server(),
            {
                "id": "self-labeled",
                "title": "Exciting. official video",
                "channel": "Anh Loc official",
            },
        )

        self.assertLess(score, 0.0)

    def test_provider_official_video_type_beats_user_upload(self) -> None:
        search_server = SearchService(server)._search_server()
        official_score = source_quality_score(
            search_server,
            {
                "id": "official",
                "title": "Bohemian Rhapsody",
                "channel": "Queen",
                "video_type": "MUSIC_VIDEO_TYPE_OMV",
            },
        )
        upload_score = source_quality_score(
            search_server,
            {
                "id": "upload",
                "title": "Bohemian Rhapsody",
                "channel": "Cover Channel",
                "video_type": "MUSIC_VIDEO_TYPE_UGC",
            },
        )
        self.assertGreater(official_score, upload_score)

    def test_candidate_observations_store_trusted_sources_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "observations.sqlite"))

            weak_count = remember_candidate_observations(
                memory_server,
                user_scope_id="guest",
                query="excitin",
                items=[
                    {
                        "id": "weak",
                        "title": "Excitin",
                        "channel": "RG Ironic",
                    }
                ],
            )
            trusted_count = remember_candidate_observations(
                memory_server,
                user_scope_id="guest",
                query="November Rain",
                items=[
                    {
                        "id": "topic",
                        "title": "November Rain",
                        "channel": "Guns N' Roses - Topic",
                        "channel_id": "UC-topic",
                    }
                ],
            )
            memories = load_catalog_entity_memories(
                memory_server,
                query="gnr november rain",
                entity_type="track",
            )

            self.assertEqual(weak_count, 0)
            self.assertEqual(trusted_count, 1)
            self.assertTrue(memories)
            self.assertEqual(memories[0].get("artist_key"), "guns n roses")

    def test_query_memory_boosts_previously_resolved_entity(self) -> None:
        query = "shared title memory test"
        user_scope_id = "search-memory-test-user"
        remembered = {
            "id": "remembered",
            "title": "Shared Title Memory Test",
            "channel": "Correct Artist",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "memory.sqlite"))

            self.assertTrue(
                remember_search_resolution(
                    memory_server,
                    user_scope_id=user_scope_id,
                    query=query,
                    entity_type="track",
                    item=remembered,
                    confidence=0.96,
                    event_weight=2.5,
                    source="unit_test",
                )
            )
            memories = load_query_memory(
                memory_server,
                user_scope_id=user_scope_id,
                query=query,
            )
            self.assertTrue(memories)

            result = resolve_canonical_tracks(
                SearchServerAdapter(memory_server),
                query,
                [
                    {
                        "id": "wrong",
                        "title": "Shared Title Memory Test",
                        "channel": "Wrong Artist",
                    },
                    remembered,
                ],
                limit=2,
                memories=memories,
            )
            ordered = result.tracks

            self.assertEqual(ordered[0].get("id"), "remembered")
            self.assertGreater(
                (ordered[0].get("ranking_features") or {}).get("query_memory_boost") or 0.0,
                0.0,
            )

    def test_authoritative_catalog_memory_beats_wrong_live_exact_title(self) -> None:
        user_scope_id = "authoritative-memory-user"
        correct = {
            "id": "correct",
            "title": "Excitin",
            "channel": "Cece Natalie - Topic",
            "channel_id": "UC-cece-topic",
            "source_authority": "official",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "authoritative.sqlite"))

            self.assertTrue(
                remember_search_resolution(
                    memory_server,
                    user_scope_id=user_scope_id,
                    query="excitin",
                    entity_type="track",
                    item=correct,
                    confidence=0.97,
                    event_weight=3.0,
                    source="selected_search_result",
                )
            )
            memories = load_query_memory(
                memory_server,
                user_scope_id=user_scope_id,
                query="excitin",
            )

            result = resolve_canonical_tracks(
                SearchServerAdapter(memory_server),
                "excitin",
                [
                    {
                        "id": "wrong",
                        "title": "Excitin",
                        "channel": "RG Ironic",
                    },
                    correct,
                ],
                limit=2,
                memories=memories,
            )

            self.assertEqual(result.tracks[0].get("id"), "correct")
            wrong_features = next(
                track.get("ranking_features") or {}
                for track in result.tracks
                if track.get("id") == "wrong"
            )
            self.assertGreater(
                wrong_features.get("canonical_entity_mismatch_penalty") or 0.0,
                0.0,
            )

    def test_query_alias_boosts_alternate_wording_to_canonical_entity(self) -> None:
        user_scope_id = "search-alias-test-user"
        canonical_track = {
            "id": "canonical",
            "title": "November Rain",
            "channel": "Guns N' Roses - Topic",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "alias.sqlite"))

            self.assertTrue(
                remember_search_resolution(
                    memory_server,
                    user_scope_id=user_scope_id,
                    query="November Rain",
                    entity_type="track",
                    item=canonical_track,
                    confidence=0.95,
                    event_weight=2.0,
                    source="unit_test_alias",
                )
            )
            aliases = load_query_aliases(memory_server, query="gnr november rain")
            self.assertTrue(aliases)

            result = resolve_canonical_tracks(
                SearchServerAdapter(memory_server),
                "gnr november rain",
                [
                    {
                        "id": "wrong",
                        "title": "November Rain",
                        "channel": "Different Artist",
                    },
                    canonical_track,
                ],
                limit=2,
                memories=aliases,
            )

            self.assertEqual(result.tracks[0].get("id"), "canonical")

    def test_source_identity_cache_annotates_topic_channel_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "source.sqlite"))
            item = {
                "id": "topic-track",
                "playback": {"provider": "youtube", "source_id": "00000000001"},
                "source_provider": "youtube",
                "title": "Purple Rain",
                "channel": "Prince & The Revolution - Topic",
                "channel_id": "UC-topic-prince",
            }

            self.assertTrue(remember_source_identity(memory_server, item))
            annotated = annotate_source_identity(
                memory_server,
                {
                    "id": "candidate",
                    "title": "Purple Rain",
                    "channel": "Prince & The Revolution - Topic",
                    "channel_id": "UC-topic-prince",
                },
            )

            self.assertEqual(annotated.get("source_identity_authority"), "topic")
            self.assertEqual(annotated.get("source_authority"), "official")
            self.assertGreaterEqual(
                source_quality_score(SearchServerAdapter(memory_server), annotated),
                1.0,
            )

    def test_batch_source_identity_backfill_marks_official_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "source-batch.sqlite"))
            items = [
                {
                    "id": "topic-track",
                    "playback": {"provider": "youtube", "source_id": "00000000001"},
                    "source_provider": "youtube",
                    "title": "Bring Me To Life",
                    "channel": "Evanescence - Topic",
                    "channel_id": "UC-topic-evanescence",
                },
                {
                    "id": "vevo-track",
                    "playback": {"provider": "youtube", "source_id": "00000000002"},
                    "source_provider": "youtube",
                    "title": "Bring Me To Life",
                    "channel": "EvanescenceVEVO",
                    "channel_id": "UC-vevo-evanescence",
                },
                {
                    "id": "weak",
                    "title": "Bring Me To Life",
                    "channel": "Some unofficial cover channel",
                },
            ]

            stored = remember_source_identities(memory_server, items)
            annotated = annotate_source_identity(memory_server, dict(items[0]))

            self.assertGreaterEqual(stored, 2)
            self.assertEqual(annotated.get("source_identity_authority"), "topic")
            self.assertEqual(annotated.get("source_authority"), "official")

    def test_played_search_resolution_adds_learned_popularity_to_canonical_entity(self) -> None:
        user_scope_id = "search-learned-popularity-user"
        canonical_track = {
            "id": "evanescence",
            "title": "Bring Me To Life",
            "channel": "Evanescence - Topic",
            "channel_id": "UC-topic-evanescence",
        }
        weak_track = {
            "id": "weak",
            "title": "Bring Me To Life",
            "channel": "Echoes Of Asgard",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "learned.sqlite"))

            self.assertTrue(
                remember_search_resolution(
                    memory_server,
                    user_scope_id=user_scope_id,
                    query="Bring Me To Life",
                    entity_type="track",
                    item=canonical_track,
                    confidence=0.96,
                    event_weight=3.0,
                    event_type="play_start",
                    source="unit_test_play",
                )
            )
            entity = load_canonical_entity(
                memory_server,
                entity_type="track",
                item=canonical_track,
            )
            self.assertEqual(entity.get("play_count"), 1)
            self.assertGreater(entity.get("learned_popularity") or 0.0, 0.0)
            self.assertEqual(entity.get("official_source_authority"), "topic")

            annotated_canonical = annotate_canonical_entity(memory_server, canonical_track)
            memories = load_query_memory(
                memory_server,
                user_scope_id=user_scope_id,
                query="bring me to life",
            )
            result = resolve_canonical_tracks(
                SearchServerAdapter(memory_server),
                "bring me to life",
                [weak_track, annotated_canonical],
                limit=2,
                memories=memories,
            )

            self.assertEqual(result.tracks[0].get("id"), "evanescence")
            self.assertGreater(result.tracks[0].get("learned_popularity") or 0.0, 0.0)

    def test_query_aliases_cover_artist_title_permutations(self) -> None:
        user_scope_id = "search-alias-permutation-user"
        canonical_track = {
            "id": "november-rain",
            "title": "November Rain",
            "channel": "Guns N' Roses - Topic",
            "channel_id": "UC-topic-gnr",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "alias-permutation.sqlite"))

            self.assertTrue(
                remember_search_resolution(
                    memory_server,
                    user_scope_id=user_scope_id,
                    query="November Rain",
                    entity_type="track",
                    item=canonical_track,
                    confidence=0.97,
                    event_weight=2.0,
                    event_type="play_start",
                    source="unit_test_alias_permutation",
                )
            )

            for alias_query in ("gnr november rain", "guns roses november rain", "november rain gnr"):
                aliases = load_query_aliases(memory_server, query=alias_query)
                self.assertTrue(aliases, alias_query)
                result = resolve_canonical_tracks(
                    SearchServerAdapter(memory_server),
                    alias_query,
                    [
                        {
                            "id": "wrong",
                            "title": "November Rain",
                            "channel": "Different Artist",
                        },
                        canonical_track,
                    ],
                    limit=2,
                    memories=aliases,
                )
                self.assertEqual(result.tracks[0].get("id"), "november-rain")

    def test_selected_result_aliases_include_raw_and_stripped_title_variants(self) -> None:
        user_scope_id = "search-alias-raw-selected-user"
        canonical_track = {
            "id": "bring-me-to-life",
            "title": "Bring Me To Life (Official Music Video)",
            "artist": "Evanescence",
            "channel": "EvanescenceVEVO",
            "channel_id": "UC-vevo-evanescence",
            "aliases": ["evanescence bring life"],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "alias-raw.sqlite"))

            self.assertTrue(
                remember_search_resolution(
                    memory_server,
                    user_scope_id=user_scope_id,
                    query="bring me life",
                    entity_type="track",
                    item=canonical_track,
                    confidence=0.96,
                    event_weight=2.5,
                    event_type="result_play",
                    source="search_interaction",
                )
            )

            for alias_query in (
                "evanescence bring me to life",
                "bring me to life evanescence",
                "evanescence bring life",
            ):
                aliases = load_query_aliases(memory_server, query=alias_query)
                self.assertTrue(aliases, alias_query)
                result = resolve_canonical_tracks(
                    SearchServerAdapter(memory_server),
                    alias_query,
                    [
                        {
                            "id": "wrong",
                            "title": "Bring Me To Life",
                            "artist": "Echoes Of Asgard",
                        },
                        canonical_track,
                    ],
                    limit=2,
                    memories=aliases,
                )
                self.assertEqual(result.tracks[0].get("id"), "bring-me-to-life")

    def test_catalog_entity_registry_reuses_learned_aliases_for_new_queries(self) -> None:
        canonical_track = {
            "id": "canonical",
            "title": "November Rain",
            "channel": "Guns N' Roses - Topic",
            "channel_id": "UC-topic-gnr",
        }
        weak_track = {
            "id": "weak",
            "title": "November Rain",
            "channel": "Different Artist",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "catalog-registry.sqlite"))

            self.assertTrue(
                remember_catalog_entity(
                    memory_server,
                    user_scope_id="catalog-user",
                    query="November Rain",
                    entity_type="track",
                    item=canonical_track,
                    confidence=0.97,
                    event_weight=2.0,
                    event_type="play_start",
                    source="unit_test_catalog",
                )
            )
            memories = load_catalog_entity_memories(
                memory_server,
                query="guns roses november rain",
                entity_type="track",
            )
            self.assertTrue(memories)
            self.assertEqual(
                memories[0].get("entity_key"),
                catalog_entity_key("track", canonical_track, query="November Rain"),
            )

            result = resolve_canonical_tracks(
                SearchServerAdapter(memory_server),
                "guns roses november rain",
                [weak_track, canonical_track],
                limit=2,
                memories=memories,
            )

            self.assertEqual(result.tracks[0].get("id"), "canonical")

    def test_broad_search_hydration_cannot_poison_exact_query_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(
                str(pathlib.Path(tmp_dir) / "catalog-alias-repair.sqlite")
            )
            wrong_track = {
                "id": "queen-catalog-track",
                "title": "Another One Bites the Dust",
                "channel": "Queen",
            }
            exact_track = {
                "id": "iron-maiden-trooper",
                "title": "The Trooper",
                "channel": "Iron Maiden",
            }
            self.assertTrue(
                remember_catalog_entity(
                    memory_server,
                    user_scope_id="global",
                    query="The Trooper",
                    entity_type="track",
                    item=wrong_track,
                    confidence=0.84,
                    event_weight=0.0,
                    event_type="search_catalog_hydration",
                    source="canonical_search_result",
                )
            )
            self.assertTrue(
                remember_catalog_entity(
                    memory_server,
                    user_scope_id="global",
                    query="The Trooper",
                    entity_type="track",
                    item=exact_track,
                    confidence=0.84,
                    event_weight=0.0,
                    event_type="search_catalog_hydration",
                    source="canonical_search_result",
                )
            )

            removed = remove_untrusted_catalog_query_aliases(
                memory_server,
                query="The Trooper",
            )
            memories = load_catalog_entity_memories(
                memory_server,
                query="The Trooper",
                entity_type="track",
                limit=8,
            )

            self.assertEqual(removed, 1)
            self.assertEqual(
                [memory.get("payload", {}).get("id") for memory in memories],
                ["iron-maiden-trooper"],
            )

    def test_catalog_hydration_learns_intrinsic_aliases_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(
                str(pathlib.Path(tmp_dir) / "catalog-intrinsic-alias.sqlite")
            )
            track = {
                "id": "queen-catalog-track",
                "title": "Another One Bites the Dust",
                "channel": "Queen",
            }
            self.assertTrue(
                remember_catalog_entity(
                    memory_server,
                    user_scope_id="global",
                    query="The Trooper",
                    entity_type="track",
                    item=track,
                    confidence=0.84,
                    event_weight=0.0,
                    event_type="search_catalog_hydration",
                    source="canonical_search_result",
                    learn_query_alias=False,
                )
            )

            self.assertEqual(
                load_catalog_entity_memories(
                    memory_server,
                    query="The Trooper",
                    entity_type="track",
                ),
                [],
            )
            intrinsic = load_catalog_entity_memories(
                memory_server,
                query="Another One Bites the Dust",
                entity_type="track",
            )
            self.assertEqual(
                [memory.get("payload", {}).get("id") for memory in intrinsic],
                ["queen-catalog-track"],
            )

    def test_search_resolution_writes_catalog_entity_and_source_link(self) -> None:
        canonical_track = {
            "id": "evanescence",
            "title": "Bring Me To Life",
            "channel": "EvanescenceVEVO",
            "channel_id": "UC-vevo-evanescence",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "catalog-resolution.sqlite"))

            self.assertTrue(
                remember_search_resolution(
                    memory_server,
                    user_scope_id="catalog-resolution-user",
                    query="bring me life evanescence",
                    entity_type="track",
                    item=canonical_track,
                    confidence=0.96,
                    event_weight=3.0,
                    event_type="play_start",
                    source="unit_test_catalog_resolution",
                )
            )
            memories = load_catalog_entity_memories(
                memory_server,
                query="evanescence bring me life",
                entity_type="track",
            )

            self.assertTrue(memories)
            self.assertEqual(
                memories[0].get("entity_key"),
                catalog_entity_key("track", canonical_track, query="bring me life evanescence"),
            )

    def test_canonical_backfill_promotes_older_search_interaction_events(self) -> None:
        canonical_track = {
            "id": "november-rain-topic",
            "title": "November Rain",
            "channel": "Guns N' Roses - Topic",
            "channel_id": "UC-topic-gnr",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "backfill-events.sqlite"))
            connection = open_recommendation_store_connection(memory_server)
            try:
                connection.execute(
                    """
                    INSERT INTO recommendation_search_events(
                        id, user_scope_id, query, result_count, source,
                        metadata_json, occurred_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        "event-1",
                        "user-1",
                        "gnr november rain",
                        5,
                        "play_start",
                        json.dumps(
                            {
                                "selected_entity_type": "track",
                                "selected_item": canonical_track,
                                "event_type": "play_start",
                                "confidence": 0.96,
                            },
                            ensure_ascii=False,
                        ),
                        time.time(),
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            result = backfill_canonical_catalog(memory_server)
            memories = load_catalog_entity_memories(
                memory_server,
                query="guns roses november rain",
                entity_type="track",
            )

            self.assertEqual(result.get("processed_search_events"), 1)
            self.assertTrue(memories)
            self.assertEqual(memories[0].get("artist_key"), "guns n roses")

    def test_canonical_backfill_replays_trusted_canonical_rows_into_alias_registry(self) -> None:
        canonical_track = {
            "id": "evanescence-topic",
            "playback": {"provider": "youtube", "source_id": "00000000001"},
            "title": "Bring Me To Life",
            "channel": "Evanescence - Topic",
            "channel_id": "UC-topic-evanescence",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "backfill-canonical.sqlite"))
            connection = open_recommendation_store_connection(memory_server)
            try:
                connection.execute(
                    """
                    INSERT INTO search_canonical_entities(
                        entity_type, entity_key, title_key, artist_key, album_key,
                        source_authority, source_quality, popularity,
                        click_count, play_count, skip_count, payload_json,
                        official_source_provider, official_source_key,
                        official_source_authority, official_confidence,
                        learned_popularity, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        "track",
                        "bring me to life|evanescence",
                        "bring me to life",
                        "evanescence",
                        "fallen",
                        "official",
                        1.2,
                        0.8,
                        2,
                        1,
                        0,
                        json.dumps(canonical_track, ensure_ascii=False),
                        "youtube",
                        "UC-topic-evanescence",
                        "topic",
                        0.92,
                        0.28,
                        time.time(),
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            result = backfill_canonical_catalog(memory_server)
            memories = load_catalog_entity_memories(
                memory_server,
                query="evanescence bring me life",
                entity_type="track",
            )

            self.assertEqual(result.get("processed_canonical_entities"), 1)
            self.assertTrue(memories)
            self.assertEqual(memories[0].get("artist_key"), "evanescence")



    def test_unknown_album_artist_receives_hard_quality_penalty(self) -> None:
        penalty = album_result_penalty(
            server,
            {"title": "Purple Rain", "artist": "Unknown Artist"},
            query="Purple Rain",
        )
        self.assertGreaterEqual(penalty, 5.0)


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

    @patch("auralis_backend.search.service.search_canonical_album_for_track")
    def test_accepted_track_hydrates_exact_containing_album(
        self,
        mock_album_lookup,
    ) -> None:
        mock_album_lookup.return_value = {
            "id": "MPREb_everlong",
            "provider_album_id": "MPREb_everlong",
            "title": "The Colour and the Shape",
            "artist": "Foo Fighters",
            "thumbnail": "https://example.test/colour.jpg",
        }

        album = search_service_module._hydrate_containing_album_from_accepted_target(
            {
                "title": "The Colour and the Shape",
                "artist": "Foo Fighters",
                "musicbrainz_release_group_id": "mb-release-group",
                "playable": False,
            },
            [],
            {"id": "UC-FooFighters", "name": "Foo Fighters"},
            server=server,
        )

        self.assertEqual(album.get("id"), "MPREb_everlong")
        self.assertEqual(
            album.get("musicbrainz_release_group_id"),
            "mb-release-group",
        )


    def test_user_history_catalog_seed_creates_playable_alias_memory(self) -> None:
        canonical_track = {
            "id": "evanescence-topic",
            "playback": {"provider": "youtube", "source_id": "00000000001"},
            "title": "Bring Me To Life",
            "artist": "Evanescence",
            "channel": "Evanescence - Topic",
            "channel_id": "UC-topic-evanescence",
            "album": "Fallen",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "history-seed.sqlite"))
            result = populate_catalog_from_user_signals(
                memory_server,
                user_scope_id="user-1",
                req=SimpleNamespace(
                    last_played_tracks=[canonical_track],
                    recent_track_snapshots=[],
                    top_track_snapshots=[],
                ),
            )

            playable = catalog_playable_tracks_for_query(
                memory_server,
                user_scope_id="user-1",
                query="bring me to life",
            )

            self.assertEqual(result.get("seed_tracks"), 1)
            self.assertGreater(result.get("stored_track_aliases"), 0)
            self.assertEqual(playable[0].get("id"), "evanescence-topic")


    def test_catalog_population_scheduler_runs_inline_without_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            memory_server = _MemoryTestServer(str(pathlib.Path(tmp_dir) / "schedule-inline.sqlite"))
            result = schedule_catalog_population(
                memory_server,
                user_scope_id="user-1",
                req=SimpleNamespace(
                    last_played_tracks=[
                        {
                            "id": "radiohead-creep",
                            "playback": {
                                "provider": "youtube",
                                "source_id": "00000000001",
                            },
                            "title": "Creep",
                            "artist": "Radiohead",
                            "channel": "Radiohead - Topic",
                        }
                    ],
                ),
                reason="unit_test",
                min_interval_seconds=0.0,
            )
            playable = catalog_playable_tracks_for_query(
                memory_server,
                user_scope_id="user-1",
                query="creep radiohead",
            )

            self.assertEqual(result.get("reason"), "ran_inline")
            self.assertEqual(playable[0].get("id"), "radiohead-creep")

if __name__ == "__main__":
    unittest.main()
