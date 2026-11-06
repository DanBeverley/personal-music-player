from __future__ import annotations

import pathlib
import sys
import threading
import time
from types import SimpleNamespace


CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.charts import service as charts_module  # noqa: E402
from auralis_backend.charts.service import ChartsService  # noqa: E402
from auralis_backend.discovery.enrichment import (  # noqa: E402
    MaterializedCandidateSupply,
)


def _server(path: pathlib.Path):
    return SimpleNamespace(
        RECOMMENDATION_STORE_DB_PATH=str(path),
        recommendation_store_lock=threading.RLock(),
    )


def _provider_rows(offset: int = 0):
    listenbrainz = [
        {
            "title": f"Song {index}",
            "artist": f"Artist {index}",
            "chart_rank": ((index + offset) % 12) + 1,
            "chart_provider": "listenbrainz",
        }
        for index in range(12)
    ]
    lastfm = [
        {
            "title": f"Song {index}",
            "artist": f"Artist {index}",
            "chart_rank": 12 - ((index + offset) % 12),
            "chart_provider": "lastfm",
            "listen_count": 1000 - index,
        }
        for index in range(12)
    ]
    return {"listenbrainz": listenbrainz, "lastfm": lastfm}


def _verified_supply(_server, supply, _taste, **_kwargs):
    entries = []
    for index, item in enumerate(supply.pools["popularity"]):
        entries.append(
            {
                **item,
                "id": f"video{index:06d}"[-11:],
                "videoId": f"video{index:06d}"[-11:],
                "playable_source_id": f"video{index:06d}"[-11:],
                "playable": True,
                "thumbnail": f"https://images.example/{index}.jpg",
            }
        )
    return MaterializedCandidateSupply(pools={"popularity": entries})


def test_refresh_persists_playable_chart_and_reports_rank_history(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    service = ChartsService(_server(tmp_path / "charts.sqlite"))
    monkeypatch.setattr(charts_module, "verify_materialized_supply", _verified_supply)
    monkeypatch.setattr(
        service,
        "_collect",
        lambda _country, _range: (
            _provider_rows(),
            {"listenbrainz": {"status": "ok"}, "lastfm": {"status": "ok"}},
        ),
    )

    first = service.refresh(country="GLOBAL", range_key="week")
    assert first["published"] is True
    assert first["count"] == 12

    time.sleep(0.01)
    monkeypatch.setattr(
        service,
        "_collect",
        lambda _country, _range: (
            _provider_rows(offset=4),
            {"listenbrainz": {"status": "ok"}, "lastfm": {"status": "ok"}},
        ),
    )
    second = service.refresh(country="GLOBAL", range_key="week")
    assert second["published"] is True

    payload = service.get(
        country="GLOBAL",
        range_key="week",
        source="listenbrainz",
        limit=10,
    )
    assert payload["status"] == "ready"
    assert len(payload["entries"]) == 10
    assert all(
        "listenbrainz" in entry["chart_providers"]
        and entry["playable"] is True
        and len(entry["history"]) == 2
        for entry in payload["entries"]
    )
    assert any(entry["rank_change"] != 0 for entry in payload["entries"])


def test_empty_chart_returns_warming_without_blocking_on_provider_io(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    service = ChartsService(_server(tmp_path / "charts.sqlite"))
    scheduled = []
    monkeypatch.setattr(
        service,
        "ensure_refresh",
        lambda **kwargs: scheduled.append(kwargs) or True,
    )

    payload = service.get(country="US", range_key="month", source="youtube")

    assert payload["status"] == "warming"
    assert payload["entries"] == []
    assert scheduled == [{"country": "US", "range_key": "month"}]


def test_balanced_publication_preserves_provider_specific_depth(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    service = ChartsService(_server(tmp_path / "balanced.sqlite"))
    monkeypatch.setattr(charts_module, "verify_materialized_supply", _verified_supply)
    provider_rows = {
        provider: [
            {
                "title": f"{provider} Song {index}",
                "artist": f"{provider} Artist {index}",
                "chart_rank": index + 1,
                "chart_provider": provider,
            }
            for index in range(50)
        ]
        for provider in ("listenbrainz", "lastfm", "youtube")
    }
    monkeypatch.setattr(
        service,
        "_collect",
        lambda _country, _range: (
            provider_rows,
            {
                provider: {"status": "ok", "count": len(rows)}
                for provider, rows in provider_rows.items()
            },
        ),
    )

    result = service.refresh(country="GLOBAL", range_key="week")

    assert result["published"] is True
    assert result["count"] == 120
    for provider in provider_rows:
        payload = service.get(
            country="GLOBAL",
            range_key="week",
            source=provider,
            limit=50,
        )
        assert len(payload["entries"]) == 40


def test_refresh_verifies_each_provider_with_an_independent_budget(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    service = ChartsService(_server(tmp_path / "provider-budgets.sqlite"))
    profiles = []

    def verify(_server, supply, taste, **_kwargs):
        profiles.append(taste.profile_key)
        return _verified_supply(_server, supply, taste)

    monkeypatch.setattr(charts_module, "verify_materialized_supply", verify)
    provider_rows = {
        provider: [
            {
                "title": f"{provider} Song {index}",
                "artist": f"{provider} Artist {index}",
                "chart_rank": index + 1,
                "chart_provider": provider,
            }
            for index in range(12)
        ]
        for provider in ("listenbrainz", "lastfm", "youtube")
    }
    monkeypatch.setattr(
        service,
        "_collect",
        lambda _country, _range: (
            provider_rows,
            {provider: {"status": "ok"} for provider in provider_rows},
        ),
    )

    result = service.refresh(country="GLOBAL", range_key="week")

    assert result["published"] is True
    assert len(profiles) == 3
    assert all(any(profile.endswith(provider) for profile in profiles) for provider in provider_rows)
    assert all(
        result["provider_status"][provider]["playable_count"] == 12
        for provider in provider_rows
    )


def test_chart_prewarm_targets_global_and_recent_persisted_demand(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    path = tmp_path / "demand.sqlite"
    first = ChartsService(_server(path))
    first._repository.record_demand("VN", "week", at=time.time())
    service = ChartsService(_server(path))
    requested = []
    monkeypatch.setattr(
        service,
        "ensure_refresh",
        lambda **kwargs: requested.append(
            (kwargs["country"], kwargs["range_key"])
        )
        or True,
    )

    scheduled = service.ensure_demand_refreshes()

    assert scheduled == 2
    assert requested == [("GLOBAL", "week"), ("VN", "week")]


def test_regional_collection_does_not_label_sitewide_listenbrainz_as_local(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    service = ChartsService(_server(tmp_path / "regional.sqlite"))
    called = []
    monkeypatch.setattr(service, "_listenbrainz", lambda _range: called.append("lb") or [])
    monkeypatch.setattr(service, "_lastfm", lambda _country: [])
    monkeypatch.setattr(service, "_youtube", lambda _country: [])

    results, status = service._collect("US", "week")

    assert "listenbrainz" not in results
    assert "listenbrainz" not in status
    assert called == []


def test_lastfm_placeholder_artwork_is_replaced_after_source_verification(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    service = ChartsService(_server(tmp_path / "lastfm-artwork.sqlite"))

    class FakeLastFm:
        def __init__(self, _server):
            pass

        def chart_tracks(self, **_kwargs):
            return [{
                "title": "Song",
                "artist": "Artist",
                "thumbnail": "https://lastfm-img.example/2a96cbd8b46e442fc41c2b86b821562f.png",
            }]

    monkeypatch.setattr(charts_module, "LastFmClient", FakeLastFm)

    rows = service._lastfm("GLOBAL")

    assert rows[0]["thumbnail"] == ""


def test_youtube_chart_adapter_skips_malformed_shelf_and_returns_full_playlist(
    tmp_path: pathlib.Path,
) -> None:
    class FakeYtMusic:
        def get_charts(self, **_kwargs):
            return {
                "videos": [
                    {"title": "Trending albums", "playlistId": "broken"},
                    {"title": "Top 100 Music Videos", "playlistId": "valid"},
                ]
            }

        def get_playlist(self, playlist_id, **_kwargs):
            if playlist_id == "broken":
                raise TypeError("not a playlist shelf")
            return {
                "tracks": [
                    {
                        "videoId": f"chart{id_:06d}",
                        "title": f"Track {id_}",
                        "artists": [{"name": f"Artist {id_}"}],
                        "thumbnails": [{"url": f"https://img/{id_}.jpg"}],
                    }
                    for id_ in range(3)
                ]
            }

    server = _server(tmp_path / "youtube.sqlite")
    server.ytmusic = FakeYtMusic()
    service = ChartsService(server)

    rows = service._youtube("VN")

    assert len(rows) == 3
    assert [row["chart_rank"] for row in rows] == [1, 2, 3]
    assert all(row["thumbnail"].startswith("https://img/") for row in rows)
