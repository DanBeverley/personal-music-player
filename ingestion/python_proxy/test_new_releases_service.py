from __future__ import annotations

import pathlib
import sys
import threading
from datetime import date
import pytest
from types import SimpleNamespace


CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.releases import service as releases_module  # noqa: E402
from auralis_backend.releases.service import NewReleasesService  # noqa: E402


@pytest.fixture(autouse=True)
def release_details(monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 7)
    monkeypatch.setattr(releases_module, "date", Today)
    def details(_server, album_id):
        return {"id": album_id, "views": "20K", "tracks": [
            {"id": "track000001", "title": "New Track", "channel": "A New Artist", "views": "12K"}
        ]}
    monkeypatch.setattr(
        "auralis_backend.details.detail_runtime.build_album_details_payload", details
    )


class _YTMusic:
    def get_explore(self):
        return {
            "new_releases": [
                {
                    "title": "A New Album",
                    "type": "Album",
                    "browseId": "MPREb_release",
                    "artists": [{"name": "A New Artist", "id": "artist"}],
                    "thumbnails": [{"url": "https://images.example/album.jpg"}],
                    "year": "2026",
                }
            ]
        }


class _ListenBrainz:
    def __init__(self, _server):
        pass

    def fresh_releases(self, *, days=45):
        assert days in (45, 90)
        return [
            {
                "release_name": "A New Album",
                "artist_credit_name": "A New Artist",
                "release_date": "2026-08-29",
                "release_mbid": "release-mbid",
                "release_group_mbid": "group-mbid",
            }
        ]


class _YTMusicFallback:
    def get_explore(self):
        return {"moods_and_genres": []}

    def search(self, query, *, filter, limit):
        assert filter == "albums"
        assert limit == 4
        return [
            {
                "title": "A New Album",
                "browseId": "MPREb_resolved",
                "artists": [{"name": "A New Artist", "id": "artist"}],
                "thumbnails": [{"url": "https://images.example/resolved.jpg"}],
            }
        ]


def test_new_releases_are_detail_ready_and_canonically_enriched(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    server = SimpleNamespace(
        RECOMMENDATION_STORE_DB_PATH=str(tmp_path / "releases.sqlite"),
        recommendation_store_lock=threading.RLock(),
        ytmusic=_YTMusic(),
    )
    monkeypatch.setattr(releases_module, "ListenBrainzClient", _ListenBrainz)
    service = NewReleasesService(server)

    payload = service.get(limit=10)

    assert payload["status"] == "ready"
    assert len(payload["entries"]) == 2
    release = next(entry for entry in payload["entries"] if entry["entry_type"] == "album")
    assert release["id"] == "MPREb_release"
    assert release["detail_ready"] is True
    assert release["thumbnail"] == "https://images.example/album.jpg"
    assert release["release_date"] == "2026-08-29"
    assert release["release_providers"] == ["ytmusic", "listenbrainz"]


def test_fresh_release_evidence_resolves_when_explore_omits_release_shelf(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    server = SimpleNamespace(
        RECOMMENDATION_STORE_DB_PATH=str(tmp_path / "fallback.sqlite"),
        recommendation_store_lock=threading.RLock(),
        ytmusic=_YTMusicFallback(),
    )
    monkeypatch.setattr(releases_module, "ListenBrainzClient", _ListenBrainz)

    payload = NewReleasesService(server).get(limit=10)

    assert payload["status"] == "ready"
    assert next(entry for entry in payload["entries"] if entry["entry_type"] == "album")["id"] == "MPREb_resolved"
    assert payload["entries"][0]["release_date"] == "2026-08-29"


def test_release_without_detail_tracks_is_not_published(tmp_path, monkeypatch):
    monkeypatch.setattr(releases_module, "ListenBrainzClient", _ListenBrainz)
    monkeypatch.setattr("auralis_backend.details.detail_runtime.build_album_details_payload",
                        lambda *_: {"tracks": []})
    server = SimpleNamespace(RECOMMENDATION_STORE_DB_PATH=str(tmp_path / "empty.sqlite"),
        recommendation_store_lock=threading.RLock(), ytmusic=_YTMusic())
    assert NewReleasesService(server).get()["entries"] == []


def test_fresh_release_lookup_spreads_budget_across_months():
    class Provider:
        def search(self, query, **_kwargs):
            return [{"title": query.removesuffix(" Artist"), "artists": [{"name": "Artist"}],
                     "browseId": query}]
    service = object.__new__(NewReleasesService)
    service._server = SimpleNamespace(ytmusic=Provider())
    rows = [{"release_name": str(index), "artist_credit_name": "Artist", "release_date": day}
            for index, day in enumerate(["2026-09-07"] * 8 + ["2026-08-01", "2026-07-01", "2025-01-01"])]
    result = service._resolve_fresh_releases(rows, limit=3)
    assert {entry["title"] for entry in result} == {"0", "8", "9"}


def test_background_release_requests_coalesce_without_blocking(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    server = SimpleNamespace(RECOMMENDATION_STORE_DB_PATH=str(tmp_path / "async.sqlite"),
        recommendation_store_lock=threading.RLock())
    service = NewReleasesService(server)
    calls = []
    def refresh():
        calls.append(1)
        entered.set()
        release.wait(2)
        return {"published": False}
    monkeypatch.setattr(service, "refresh", refresh)
    try:
        assert service.get(background=True)["status"] == "preparing"
        assert entered.wait(1)
        assert service.get(background=True)["status"] == "preparing"
        assert len(calls) == 1
    finally:
        release.set()


def test_release_extension_failure_keeps_new_tracks_and_reports_partial(tmp_path, monkeypatch):
    class Provider(_ListenBrainz):
        def fresh_releases(self, *, days=45):
            if days == 90:
                raise TimeoutError("large window timed out")
            return super().fresh_releases(days=days)
    monkeypatch.setattr(releases_module, "ListenBrainzClient", Provider)
    server = SimpleNamespace(RECOMMENDATION_STORE_DB_PATH=str(tmp_path / "partial.sqlite"),
        recommendation_store_lock=threading.RLock(), ytmusic=_YTMusicFallback())
    service = NewReleasesService(server)
    result = service.get()
    assert {entry["entry_type"] for entry in result["entries"]} == {"track", "album"}
    assert result["refresh"]["refresh_status"] == "partial"
    assert result["refresh"]["attempted_provider_status"]["extended"]["listenbrainz"]["error"] == "TimeoutError"


def test_failed_release_refresh_reports_failure_alongside_legacy_cache(tmp_path, monkeypatch):
    class Provider(_ListenBrainz):
        def fresh_releases(self, **_kwargs):
            raise TimeoutError()
    monkeypatch.setattr(releases_module, "ListenBrainzClient", Provider)
    server = SimpleNamespace(RECOMMENDATION_STORE_DB_PATH=str(tmp_path / "legacy.sqlite"),
        recommendation_store_lock=threading.RLock(), ytmusic=_YTMusicFallback())
    service = NewReleasesService(server)
    service._repository.publish([{"id": "old", "title": "Cached album"}], {"old": "success"})
    result = service.get(force=True)
    assert result["entries"][0]["id"] == "old"
    assert result["refresh"]["refresh_status"] == "failed"
    assert result["refresh"]["attempted_provider_status"]["recent"]["listenbrainz"]["error"] == "TimeoutError"
