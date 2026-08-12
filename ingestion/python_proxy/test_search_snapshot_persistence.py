import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auralis_backend.recommend.store_runtime import (
    init_recommendation_store,
    open_recommendation_store_connection,
)
from auralis_backend.search.service import (
    _SEARCH_SNAPSHOTS,
    _SEARCH_SNAPSHOT_MAX_ENTRIES,
    _SEARCH_SNAPSHOT_LAST_DURABLE_TOUCH,
    _SEARCH_SNAPSHOT_SERVERS,
    _learn_snapshot_alias,
    _load_search_snapshot,
    _persist_listener_snapshots,
    _resolve_snapshot_key,
    _search_snapshot_key,
    _store_search_snapshot,
    _wait_for_search_snapshot_revision,
)


pytestmark = pytest.mark.search_snapshot


def _server(path):
    return SimpleNamespace(
        RECOMMENDATION_STORE_DB_PATH=str(path),
        recommendation_store_lock=threading.RLock(),
    )


def _target_snapshot(*, identity="musicbrainz:recording:1", tracks=None):
    return {
        "revision": 1,
        "resolved_target": {
            "target_identity": identity,
            "confidence_tier": "authoritative",
            "identity_confidence": 0.9,
            "item": {"title": "Foo Bar", "artist": "Artist"},
        },
        "tracks": list(tracks or []),
    }


@pytest.fixture(autouse=True)
def _clear_memory_snapshot_cache():
    _SEARCH_SNAPSHOTS.clear()
    _SEARCH_SNAPSHOT_SERVERS.clear()
    _SEARCH_SNAPSHOT_LAST_DURABLE_TOUCH.clear()
    yield
    _SEARCH_SNAPSHOTS.clear()
    _SEARCH_SNAPSHOT_SERVERS.clear()
    _SEARCH_SNAPSHOT_LAST_DURABLE_TOUCH.clear()


def test_memory_snapshot_hit_does_not_open_sqlite_on_request_path(tmp_path):
    server = _server(tmp_path / "search.sqlite")
    key = _search_snapshot_key("", "Fast Hit", "exact")
    _store_search_snapshot(key, {"revision": 4, "tracks": [{"id": "fast"}]})
    _SEARCH_SNAPSHOT_SERVERS[key] = server

    with patch(
        "auralis_backend.search.service.open_recommendation_store_connection",
        side_effect=AssertionError("SQLite was opened synchronously"),
    ), patch(
        "auralis_backend.search.service._SEARCH_SNAPSHOT_PERSIST_EXECUTOR.submit"
    ) as submit:
        restored = _load_search_snapshot(key, server)

    assert restored["revision"] == 4
    assert restored["tracks"][0]["id"] == "fast"
    submit.assert_called_once()


def test_snapshot_survives_restart_and_is_shared(tmp_path):
    first = _server(tmp_path / "search.sqlite")
    init_recommendation_store(first)
    key = _search_snapshot_key("user-a", "Dio", "exact")
    _store_search_snapshot(
        key,
        {"revision": 3, "tracks": [{"title": "Holy Diver"}]},
        first,
    )

    _SEARCH_SNAPSHOTS.clear()
    _SEARCH_SNAPSHOT_SERVERS.clear()
    second = _server(tmp_path / "search.sqlite")
    shared_key = _search_snapshot_key("user-b", "Dio", "exact")
    restored = _load_search_snapshot(shared_key, second)

    assert restored["revision"] == 3
    assert restored["tracks"][0]["title"] == "Holy Diver"


def test_authoritative_alias_migrates_to_one_canonical_snapshot(tmp_path):
    server = _server(tmp_path / "search.sqlite")
    init_recommendation_store(server)
    query_key = _search_snapshot_key("", "Bar Foo", "exact")
    snapshot = _target_snapshot(tracks=[{"id": "foo", "title": "Foo Bar"}])
    _store_search_snapshot(query_key, snapshot, server)

    canonical_key = _learn_snapshot_alias(
        server,
        "Bar Foo",
        "exact",
        query_key,
        snapshot,
    )

    assert canonical_key.startswith("canonical-target-search-v4||target:")
    assert _resolve_snapshot_key(server, "Bar Foo", "exact") == canonical_key
    assert _load_search_snapshot(query_key, server) is None
    connection = open_recommendation_store_connection(server)
    try:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM search_snapshots"
        ).fetchone()["count"]
    finally:
        connection.close()
    assert count == 1


def test_ambiguous_target_does_not_create_snapshot_alias(tmp_path):
    server = _server(tmp_path / "search.sqlite")
    init_recommendation_store(server)
    query_key = _search_snapshot_key("", "Unclear", "exact")
    ambiguous = {
        "resolved_target": {
            "target_identity": "",
            "confidence_tier": "ambiguous",
        }
    }

    effective_key = _learn_snapshot_alias(
        server,
        "Unclear",
        "exact",
        query_key,
        ambiguous,
    )

    assert effective_key == query_key
    assert _resolve_snapshot_key(server, "Unclear", "exact") == query_key


def test_persistent_lru_retains_newest_96_snapshots(tmp_path):
    server = _server(tmp_path / "search.sqlite")
    init_recommendation_store(server)
    for index in range(_SEARCH_SNAPSHOT_MAX_ENTRIES + 4):
        key = _search_snapshot_key("", f"query {index}", "exact")
        _store_search_snapshot(key, {"revision": 1, "index": index}, server)

    connection = open_recommendation_store_connection(server)
    try:
        rows = connection.execute(
            "SELECT snapshot_key FROM search_snapshots"
        ).fetchall()
    finally:
        connection.close()
    keys = {row["snapshot_key"] for row in rows}

    assert len(keys) == _SEARCH_SNAPSHOT_MAX_ENTRIES
    assert _search_snapshot_key("", "query 0", "exact") not in keys
    assert _search_snapshot_key("", "query 3", "exact") not in keys
    assert _search_snapshot_key("", "query 99", "exact") in keys


def test_existing_canonical_snapshot_is_not_degraded_by_alias_variant(tmp_path):
    server = _server(tmp_path / "search.sqlite")
    init_recommendation_store(server)
    rich = _target_snapshot(
        tracks=[
            {"id": "foo", "title": "Foo Bar"},
            {"id": "second", "title": "Second Track"},
        ]
    )
    first_key = _search_snapshot_key("", "Foo Bar", "exact")
    _store_search_snapshot(first_key, rich, server)
    canonical_key = _learn_snapshot_alias(
        server,
        "Foo Bar",
        "exact",
        first_key,
        rich,
    )

    thin = _target_snapshot(tracks=[{"id": "foo", "title": "Foo Bar"}])
    variant_key = _search_snapshot_key("", "Bar Foo", "exact")
    _store_search_snapshot(variant_key, thin, server)
    assert (
        _learn_snapshot_alias(
            server,
            "Bar Foo",
            "exact",
            variant_key,
            thin,
        )
        == canonical_key
    )

    restored = _load_search_snapshot(canonical_key, server)
    assert [track["id"] for track in restored["tracks"]] == ["foo", "second"]


def test_legacy_alias_requires_exact_canonical_identity(tmp_path):
    server = _server(tmp_path / "search.sqlite")
    init_recommendation_store(server)
    snapshot = _target_snapshot()
    query_key = _search_snapshot_key("", "Foo Bar", "exact")
    _store_search_snapshot(query_key, snapshot, server)
    canonical_key = _learn_snapshot_alias(
        server,
        "Foo Bar",
        "exact",
        query_key,
        snapshot,
    )

    connection = open_recommendation_store_connection(server)
    try:
        connection.execute(
            """
            INSERT INTO search_query_aliases(
                alias_key, canonical_query_key, entity_type, entity_key,
                confidence, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            [
                "unsafe",
                "foo bar",
                "track",
                "recording:1",
                0.99,
                1.0,
            ],
        )
        connection.commit()
    finally:
        connection.close()

    assert _resolve_snapshot_key(server, "unsafe", "exact") != canonical_key


def test_late_snapshot_update_survives_memory_clear(tmp_path):
    server = _server(tmp_path / "search.sqlite")
    init_recommendation_store(server)
    key = _search_snapshot_key("", "Artist", "exact")
    _store_search_snapshot(key, {"revision": 1, "artists": []}, server)
    updated = {
        "revision": 2,
        "artists": [
            {
                "id": "UC-artist",
                "name": "Artist",
                "thumbnail": "/artist_artwork/verified",
            }
        ],
    }

    _persist_listener_snapshots([(key, updated)])
    _SEARCH_SNAPSHOTS.clear()
    _SEARCH_SNAPSHOT_SERVERS.clear()
    restored = _load_search_snapshot(key, _server(tmp_path / "search.sqlite"))

    assert restored["revision"] == 2
    assert restored["artists"][0]["thumbnail"] == "/artist_artwork/verified"


def test_revision_wait_returns_on_visible_change(tmp_path):
    server = _server(tmp_path / "search.sqlite")
    init_recommendation_store(server)
    key = _search_snapshot_key("", "Wait Artist", "exact")
    _store_search_snapshot(key, {"revision": 1, "artists": []}, server)

    def advance():
        _store_search_snapshot(
            key,
            {
                "revision": 2,
                "artists": [
                    {"id": "a", "name": "Wait Artist", "thumbnail": "/artist_artwork/a"}
                ],
            },
            server,
        )

    timer = threading.Timer(0.03, advance)
    timer.start()
    waited = _wait_for_search_snapshot_revision(server, key, 1, 2000)
    timer.join()
    assert waited is not None
    assert waited["revision"] > 1


def test_revision_wait_times_out_without_change(tmp_path):
    server = _server(tmp_path / "search.sqlite")
    init_recommendation_store(server)
    key = _search_snapshot_key("", "No Change", "exact")
    _store_search_snapshot(key, {"revision": 1, "artists": []}, server)
    assert _wait_for_search_snapshot_revision(server, key, 1, 30) is None
