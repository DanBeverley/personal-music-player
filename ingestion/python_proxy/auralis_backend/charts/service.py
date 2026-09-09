from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
from threading import Lock, Thread
import time
import unicodedata
import uuid
from typing import Any, Dict, Iterable, List

from ..discovery.enrichment import MaterializedCandidateSupply
from ..discovery.schema import TasteProfile
from ..discovery.source_registry import verify_materialized_supply
from ..discovery.structured_providers import (
    LastFmClient,
    ListenBrainzClient,
)
from ..recommend.store_runtime import open_recommendation_store_connection


CHART_REFRESH_SECONDS = 30 * 60
CHART_RETENTION_SECONDS = 30 * 24 * 60 * 60
CHART_MIN_PUBLISHABLE = 10
CHART_PROVIDER_LIMIT = 60
CHART_PROVIDER_RESERVE = 40
CHART_PROVIDER_NEW_VERIFICATION_LIMIT = 40
CHART_PUBLISH_LIMIT = 120
_SUPPORTED_RANGES = {"week", "month", "quarter", "year", "all_time"}
_SUPPORTED_SOURCES = {"all", "listenbrainz", "lastfm", "youtube"}
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_LASTFM_COUNTRY_NAMES = {
    "AU": "Australia",
    "BR": "Brazil",
    "CA": "Canada",
    "DE": "Germany",
    "FR": "France",
    "GB": "United Kingdom",
    "IN": "India",
    "JP": "Japan",
    "KR": "Korea, Republic of",
    "US": "United States",
    # Last.fm follows the ISO display spelling and rejects "Vietnam".
    "VN": "Viet Nam",
}

_LASTFM_PLACEHOLDER_ARTWORK_MARKERS = {
    "2a96cbd8b46e442fc41c2b86b821562f",
}


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _identity(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value).casefold())
    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", normalized).split())


def _int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _range_key(value: str) -> str:
    normalized = _text(value).casefold()
    return normalized if normalized in _SUPPORTED_RANGES else "week"


def _country_key(value: str) -> str:
    normalized = _text(value).upper()
    return normalized if re.fullmatch(r"[A-Z]{2}", normalized) else "GLOBAL"


def _source_key(value: str) -> str:
    normalized = _text(value).casefold()
    return normalized if normalized in _SUPPORTED_SOURCES else "all"


def _entity_key(item: Dict[str, Any]) -> str:
    mbid = _text(
        item.get("musicbrainz_recording_id") or item.get("recording_mbid")
    ).casefold()
    if mbid:
        return f"musicbrainz:recording:{mbid}"
    title = _identity(item.get("title") or item.get("track_name"))
    artist = _identity(item.get("artist") or item.get("artist_name"))
    return f"semantic:{title}|{artist}" if title and artist else ""


def _thumbnail(item: Dict[str, Any]) -> str:
    direct = _text(item.get("thumbnail") or item.get("image"))
    if direct:
        return direct
    images = item.get("thumbnails") or []
    urls = [
        _text(value.get("url") or value.get("#text"))
        for value in images
        if isinstance(value, dict) and _text(value.get("url") or value.get("#text"))
    ]
    return urls[-1] if urls else ""


def _artist_name(item: Dict[str, Any]) -> str:
    direct = _text(item.get("artist") or item.get("artist_name") or item.get("channel"))
    if direct:
        return direct
    artists = item.get("artists") or []
    return ", ".join(
        _text(value.get("name"))
        for value in artists
        if isinstance(value, dict) and _text(value.get("name"))
    )


class ChartsRepository:
    def __init__(self, server: Any) -> None:
        self._server = server
        self._ensure_schema()

    def _connect(self):
        return open_recommendation_store_connection(self._server)

    def _ensure_schema(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chart_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    country TEXT NOT NULL,
                    range_key TEXT NOT NULL,
                    generated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    provider_status_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_chart_snapshots_lookup
                    ON chart_snapshots(country, range_key, generated_at DESC);
                CREATE TABLE IF NOT EXISTS chart_entries (
                    snapshot_id TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    entity_key TEXT NOT NULL,
                    score REAL NOT NULL,
                    providers_json TEXT NOT NULL DEFAULT '[]',
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id, entity_key),
                    FOREIGN KEY(snapshot_id) REFERENCES chart_snapshots(snapshot_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_chart_entries_rank
                    ON chart_entries(snapshot_id, rank);
                CREATE INDEX IF NOT EXISTS idx_chart_entries_history
                    ON chart_entries(entity_key, snapshot_id);
                CREATE TABLE IF NOT EXISTS chart_demand (
                    country TEXT NOT NULL,
                    range_key TEXT NOT NULL,
                    last_requested_at REAL NOT NULL,
                    PRIMARY KEY(country, range_key)
                );
                CREATE INDEX IF NOT EXISTS idx_chart_demand_recent
                    ON chart_demand(last_requested_at DESC);
                """
            )
            connection.commit()
        finally:
            connection.close()

    def latest(self, country: str, range_key: str) -> Dict[str, Any] | None:
        connection = self._connect()
        try:
            snapshot = connection.execute(
                """
                SELECT * FROM chart_snapshots
                WHERE country = ? AND range_key = ?
                ORDER BY generated_at DESC LIMIT 1
                """,
                [country, range_key],
            ).fetchone()
            if snapshot is None:
                return None
            rows = connection.execute(
                """
                SELECT rank, entity_key, score, providers_json, payload_json
                FROM chart_entries WHERE snapshot_id = ? ORDER BY rank
                """,
                [snapshot["snapshot_id"]],
            ).fetchall()
            return {
                "snapshot_id": snapshot["snapshot_id"],
                "country": snapshot["country"],
                "range": snapshot["range_key"],
                "generated_at": float(snapshot["generated_at"]),
                "expires_at": float(snapshot["expires_at"]),
                "provider_status": json.loads(snapshot["provider_status_json"] or "{}"),
                "entries": [
                    {
                        **json.loads(row["payload_json"] or "{}"),
                        "rank": int(row["rank"]),
                        "entity_key": row["entity_key"],
                        "chart_score": float(row["score"]),
                        "chart_providers": json.loads(row["providers_json"] or "[]"),
                    }
                    for row in rows
                ],
            }
        finally:
            connection.close()

    def record_demand(self, country: str, range_key: str, *, at: float) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO chart_demand(country, range_key, last_requested_at)
                VALUES (?, ?, ?)
                ON CONFLICT(country, range_key) DO UPDATE SET
                    last_requested_at = excluded.last_requested_at
                """,
                [country, range_key, float(at)],
            )
            connection.commit()
        finally:
            connection.close()

    def recent_demand(
        self,
        *,
        since: float,
        limit: int = 4,
    ) -> List[tuple[str, str]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT country, range_key
                FROM chart_demand
                WHERE last_requested_at >= ?
                ORDER BY last_requested_at DESC
                LIMIT ?
                """,
                [float(since), max(int(limit), 1)],
            ).fetchall()
            return [
                (str(row["country"]), str(row["range_key"]))
                for row in rows
            ]
        finally:
            connection.close()

    def publish(
        self,
        *,
        country: str,
        range_key: str,
        entries: List[Dict[str, Any]],
        provider_status: Dict[str, Any],
    ) -> str:
        now = time.time()
        snapshot_id = str(uuid.uuid4())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO chart_snapshots(
                    snapshot_id, country, range_key, generated_at, expires_at,
                    provider_status_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    snapshot_id,
                    country,
                    range_key,
                    now,
                    now + CHART_REFRESH_SECONDS,
                    json.dumps(provider_status, ensure_ascii=False),
                ],
            )
            for rank, entry in enumerate(entries, start=1):
                connection.execute(
                    """
                    INSERT INTO chart_entries(
                        snapshot_id, rank, entity_key, score, providers_json,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        snapshot_id,
                        rank,
                        entry["entity_key"],
                        float(entry.get("chart_score") or 0.0),
                        json.dumps(entry.get("chart_providers") or []),
                        json.dumps(entry, ensure_ascii=False),
                    ],
                )
            cutoff = now - CHART_RETENTION_SECONDS
            expired = connection.execute(
                "SELECT snapshot_id FROM chart_snapshots WHERE generated_at < ?",
                [cutoff],
            ).fetchall()
            for row in expired:
                connection.execute(
                    "DELETE FROM chart_entries WHERE snapshot_id = ?",
                    [row["snapshot_id"]],
                )
            connection.execute(
                "DELETE FROM chart_snapshots WHERE generated_at < ?", [cutoff]
            )
            connection.commit()
            return snapshot_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def previous_ranks(
        self,
        *,
        country: str,
        range_key: str,
        before: float,
        source: str,
    ) -> Dict[str, int]:
        connection = self._connect()
        try:
            snapshots = connection.execute(
                """
                SELECT snapshot_id FROM chart_snapshots
                WHERE country = ? AND range_key = ? AND generated_at < ?
                ORDER BY generated_at DESC LIMIT 1
                """,
                [country, range_key, before],
            ).fetchone()
            if snapshots is None:
                return {}
            rows = connection.execute(
                """
                SELECT rank, entity_key, providers_json, payload_json
                FROM chart_entries
                WHERE snapshot_id = ? ORDER BY rank
                """,
                [snapshots["snapshot_id"]],
            ).fetchall()
            output: Dict[str, int] = {}
            filtered_rank = 0
            for row in rows:
                providers = json.loads(row["providers_json"] or "[]")
                if source != "all" and source not in providers:
                    continue
                filtered_rank += 1
                if source == "all":
                    output[row["entity_key"]] = int(row["rank"])
                    continue
                payload = json.loads(row["payload_json"] or "{}")
                provider_rank = _int(
                    (payload.get("provider_ranks") or {}).get(source)
                )
                output[row["entity_key"]] = provider_rank or filtered_rank
            return output
        finally:
            connection.close()

    def has_snapshot_before(
        self,
        *,
        country: str,
        range_key: str,
        before: float,
    ) -> bool:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT 1 FROM chart_snapshots
                WHERE country = ? AND range_key = ? AND generated_at < ?
                LIMIT 1
                """,
                [country, range_key, before],
            ).fetchone()
            return row is not None
        finally:
            connection.close()

    def history(
        self,
        *,
        country: str,
        range_key: str,
        entity_keys: Iterable[str],
        source: str,
        limit: int = 20,
    ) -> Dict[str, List[Dict[str, Any]]]:
        keys = list(dict.fromkeys(key for key in entity_keys if key))[:40]
        if not keys:
            return {}
        connection = self._connect()
        try:
            placeholders = ",".join("?" for _ in keys)
            rows = connection.execute(
                f"""
                SELECT snapshots.generated_at, entries.entity_key, entries.rank,
                       entries.providers_json, entries.payload_json
                FROM chart_entries AS entries
                JOIN chart_snapshots AS snapshots
                  ON snapshots.snapshot_id = entries.snapshot_id
                WHERE snapshots.country = ? AND snapshots.range_key = ?
                  AND entries.entity_key IN ({placeholders})
                ORDER BY snapshots.generated_at DESC
                """,
                [country, range_key, *keys],
            ).fetchall()
            output: Dict[str, List[Dict[str, Any]]] = {key: [] for key in keys}
            for row in rows:
                providers = json.loads(row["providers_json"] or "[]")
                if source != "all" and source not in providers:
                    continue
                values = output[row["entity_key"]]
                if len(values) >= limit:
                    continue
                values.append(
                    {
                        "at": float(row["generated_at"]),
                        "rank": (
                            int(row["rank"])
                            if source == "all"
                            else _int(
                                (
                                    json.loads(row["payload_json"] or "{}").get(
                                        "provider_ranks"
                                    )
                                    or {}
                                ).get(source)
                            )
                            or int(row["rank"])
                        ),
                    }
                )
            for values in output.values():
                values.reverse()
            return output
        finally:
            connection.close()


class ChartsService:
    def __init__(self, server: Any) -> None:
        self._server = server
        self._repository = ChartsRepository(server)
        self._refresh_lock = Lock()
        self._refreshing: set[tuple[str, str]] = set()
        self._last_refresh_status: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._demand_recorded_at: Dict[tuple[str, str], float] = {}

    def _listenbrainz(self, range_key: str) -> List[Dict[str, Any]]:
        rows = ListenBrainzClient(self._server).sitewide_recordings(
            limit=CHART_PROVIDER_LIMIT,
            range_key=range_key,
        )
        for rank, row in enumerate(rows, start=1):
            row["chart_rank"] = rank
            row["chart_provider"] = "listenbrainz"
        return rows

    def _lastfm(self, country: str) -> List[Dict[str, Any]]:
        rows = LastFmClient(self._server).chart_tracks(
            country=(
                ""
                if country == "GLOBAL"
                else _LASTFM_COUNTRY_NAMES.get(country, country)
            ),
            limit=CHART_PROVIDER_LIMIT,
        )
        for row in rows:
            thumbnail = _text(row.get("thumbnail"))
            if any(marker in thumbnail.casefold() for marker in _LASTFM_PLACEHOLDER_ARTWORK_MARKERS):
                row["thumbnail"] = ""
        return rows

    def _youtube(self, country: str) -> List[Dict[str, Any]]:
        """Read YouTube Music's own global/regional chart playlist.

        This replaces the YouTube Data API-key path.  The chart browse call
        identifies the current chart playlist and get_playlist supplies real
        track identities and artwork in one provider-owned flow.
        """

        payload = self._server.ytmusic.get_charts(
            country="ZZ" if country == "GLOBAL" else country
        ) or {}
        playlists = [value for value in payload.get("videos") or [] if isinstance(value, dict)]

        def priority(value: Dict[str, Any]) -> tuple[int, str]:
            title = _text(value.get("title")).casefold()
            if "daily top music videos" in title:
                return (0, title)
            if "top 100 music videos" in title:
                return (1, title)
            if "weekly" in title:
                return (2, title)
            if "trending" in title:
                return (3, title)
            return (4, title)

        # YTMusic's available chart shelves change by region. In particular,
        # Vietnam's first shelf is an album-style Trending collection that
        # get_playlist cannot parse. Try real music-video chart playlists in
        # priority order and isolate malformed shelves.
        rows: List[Dict[str, Any]] = []
        for selected in sorted(playlists, key=priority):
            playlist_id = _text(selected.get("playlistId"))
            if not playlist_id:
                continue
            try:
                playlist = self._server.ytmusic.get_playlist(
                    playlist_id,
                    limit=CHART_PROVIDER_LIMIT,
                ) or {}
            except (KeyError, TypeError, ValueError):
                continue
            candidate_rows = playlist.get("tracks") or []
            if candidate_rows:
                rows = [value for value in candidate_rows if isinstance(value, dict)]
                break
        output: List[Dict[str, Any]] = []
        for rank, row in enumerate(rows[:CHART_PROVIDER_LIMIT], start=1):
            if not isinstance(row, dict):
                continue
            source_id = _text(row.get("videoId") or row.get("id"))
            if not _YOUTUBE_ID.fullmatch(source_id):
                continue
            output.append(
                {
                    "id": source_id,
                    "videoId": source_id,
                    "playable_source_id": source_id,
                    "provider": "youtube",
                    "source_provider": "youtube",
                    "source_authority": "verified_catalog",
                    "title": row.get("title"),
                    "artist": _artist_name(row),
                    "channel": _artist_name(row),
                    "thumbnail": _thumbnail(row),
                    "chart_rank": rank,
                    "chart_provider": "youtube",
                    "relationship_provider": "youtube",
                    "relationship_evidence": "regional_music_chart",
                }
            )
        return output

    def _collect(self, country: str, range_key: str):
        jobs = {}
        # ListenBrainz exposes sitewide, not regional, charts. Including that
        # global list under every country made regional views appear identical.
        if country == "GLOBAL":
            jobs["listenbrainz"] = lambda: self._listenbrainz(range_key)
        # Last.fm and YouTube expose a current chart, not historical ranges.
        # Keep longer-range views honest by sourcing them from ListenBrainz.
        if range_key == "week":
            jobs["lastfm"] = lambda: self._lastfm(country)
        if range_key == "week":
            jobs["youtube"] = lambda: self._youtube(country)
        results: Dict[str, List[Dict[str, Any]]] = {}
        status: Dict[str, Any] = {}
        if not jobs:
            status["publication"] = {
                "status": "regional_range_unsupported",
                "message": "Regional providers currently expose the week chart only.",
            }
            return results, status
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            futures = {executor.submit(job): provider for provider, job in jobs.items()}
            for future in as_completed(futures):
                provider = futures[future]
                try:
                    rows = list(future.result() or [])
                    results[provider] = rows
                    status[provider] = {"status": "ok", "count": len(rows)}
                except Exception as exc:
                    results[provider] = []
                    status[provider] = {
                        "status": "failed",
                        "error": exc.__class__.__name__,
                    }
        return results, status

    @staticmethod
    def _balanced_publication(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Reserve useful depth for every successful provider before ranking fill."""

        selected: List[Dict[str, Any]] = []
        selected_keys: set[str] = set()

        def add(item: Dict[str, Any]) -> None:
            key = _text(item.get("entity_key"))
            if not key or key in selected_keys or len(selected) >= CHART_PUBLISH_LIMIT:
                return
            selected_keys.add(key)
            selected.append(item)

        for provider in ("listenbrainz", "lastfm", "youtube"):
            provider_entries = [
                item
                for item in entries
                if provider in (item.get("chart_providers") or [])
            ]
            provider_entries.sort(
                key=lambda item: _int((item.get("provider_ranks") or {}).get(provider))
                or 10_000
            )
            for item in provider_entries[:CHART_PROVIDER_RESERVE]:
                add(item)
        for item in entries:
            add(item)
        # Provider reservations decide admission only; the combined chart keeps
        # its score order instead of grouping results by provider.
        return [
            item
            for item in entries
            if _text(item.get("entity_key")) in selected_keys
        ][:CHART_PUBLISH_LIMIT]

    def _merge(self, provider_rows: Dict[str, List[Dict[str, Any]]]):
        merged: Dict[str, Dict[str, Any]] = {}
        for provider, rows in provider_rows.items():
            for fallback_rank, raw in enumerate(rows, start=1):
                item = dict(raw)
                key = _entity_key(item)
                if not key:
                    continue
                rank = max(_int(item.get("chart_rank")) or fallback_rank, 1)
                contribution = 1.0 / (20.0 + rank)
                existing = merged.get(key)
                if existing is None:
                    existing = {
                        **item,
                        "entity_key": key,
                        "chart_score": 0.0,
                        "chart_providers": [],
                        "provider_ranks": {},
                    }
                    merged[key] = existing
                if provider == "youtube" or not _text(existing.get("thumbnail")):
                    for field in (
                        "id",
                        "videoId",
                        "playable_source_id",
                        "provider",
                        "source_provider",
                        "source_authority",
                        "thumbnail",
                        "view_count",
                        "like_count",
                    ):
                        if item.get(field) not in {None, ""}:
                            existing[field] = item[field]
                existing["chart_score"] += contribution
                if provider not in existing["chart_providers"]:
                    existing["chart_providers"].append(provider)
                existing["provider_ranks"][provider] = rank
                existing["listen_count"] = max(
                    _int(existing.get("listen_count")),
                    _int(item.get("listen_count")),
                )
                existing["listener_count"] = max(
                    _int(existing.get("listener_count")),
                    _int(item.get("listener_count")),
                )
        return sorted(
            merged.values(),
            key=lambda item: (
                float(item.get("chart_score") or 0.0),
                _int(item.get("listen_count")),
                _int(item.get("view_count")),
            ),
            reverse=True,
        )

    def refresh(self, *, country: str = "GLOBAL", range_key: str = "week"):
        country = _country_key(country)
        range_key = _range_key(range_key)
        provider_rows, provider_status = self._collect(country, range_key)
        merged_candidates = self._merge(provider_rows)

        def verify_provider(provider: str, rows: List[Dict[str, Any]]):
            provider_candidates = self._merge({provider: rows})
            if not provider_candidates:
                return provider, []
            taste = TasteProfile(
                user_scope_id="charts",
                profile_key=f"charts:{country}:{range_key}:{provider}",
                signal_tier="cold_start",
            )
            verified = verify_materialized_supply(
                self._server,
                MaterializedCandidateSupply(
                    pools={"popularity": provider_candidates}
                ),
                taste,
                max_new_verifications=CHART_PROVIDER_NEW_VERIFICATION_LIMIT,
                # Provider families run concurrently; two resolver workers per
                # family keeps the combined upstream pressure bounded at six.
                max_workers=2,
                time_budget_seconds=25.0,
                hydrate_artist_artwork=False,
            )
            return provider, [
                dict(item)
                for item in verified.pools.get("popularity", [])
                if isinstance(item, dict) and item.get("playable") is True
            ]

        verified_by_provider: Dict[str, List[Dict[str, Any]]] = {}
        jobs = {
            provider: rows
            for provider, rows in provider_rows.items()
            if rows
        }
        if jobs:
            with ThreadPoolExecutor(max_workers=min(len(jobs), 3)) as executor:
                futures = {
                    executor.submit(verify_provider, provider, rows): provider
                    for provider, rows in jobs.items()
                }
                for future in as_completed(futures):
                    provider = futures[future]
                    try:
                        _, rows = future.result()
                    except Exception as exc:
                        rows = []
                        provider_status.setdefault(provider, {}).update(
                            {
                                "verification_status": "failed",
                                "verification_error": exc.__class__.__name__,
                            }
                        )
                    else:
                        provider_status.setdefault(provider, {}).update(
                            {
                                "verification_status": "ok",
                                "playable_count": len(rows),
                            }
                        )
                    verified_by_provider[provider] = rows
        playable = self._merge(verified_by_provider)
        playable.sort(
            key=lambda item: float(item.get("chart_score") or 0.0),
            reverse=True,
        )
        for item in playable:
            source_id = _text(item.get("playable_source_id") or item.get("videoId"))
            if not _text(item.get("thumbnail")) and _YOUTUBE_ID.fullmatch(source_id):
                item["thumbnail"] = f"https://i.ytimg.com/vi/{source_id}/hqdefault.jpg"
        if len(playable) < CHART_MIN_PUBLISHABLE:
            provider_status["publication"] = {
                "status": "insufficient_playable_supply",
                "count": len(playable),
                "required": CHART_MIN_PUBLISHABLE,
            }
            result = {
                "published": False,
                "count": len(playable),
                "provider_status": provider_status,
            }
            self._last_refresh_status[(country, range_key)] = result
            return result
        published_entries = self._balanced_publication(playable)
        snapshot_id = self._repository.publish(
            country=country,
            range_key=range_key,
            entries=published_entries,
            provider_status=provider_status,
        )
        print(
            "[EBB:charts] "
            f"country={country} range={range_key} snapshot={snapshot_id} "
            f"candidates={len(merged_candidates)} playable={len(playable)} "
            f"providers="
            + ",".join(
                f"{provider}:{len(rows)}"
                for provider, rows in sorted(verified_by_provider.items())
            )
        )
        result = {
            "published": True,
            "snapshot_id": snapshot_id,
            "count": len(published_entries),
            "provider_status": provider_status,
        }
        self._last_refresh_status[(country, range_key)] = result
        return result

    def ensure_refresh(
        self,
        *,
        country: str = "GLOBAL",
        range_key: str = "week",
        force: bool = False,
    ) -> bool:
        country = _country_key(country)
        range_key = _range_key(range_key)
        current = self._repository.latest(country, range_key)
        if not force and current and current["expires_at"] > time.time():
            return False
        key = (country, range_key)
        with self._refresh_lock:
            if key in self._refreshing:
                return False
            self._refreshing.add(key)

        def run() -> None:
            try:
                self.refresh(country=country, range_key=range_key)
            except Exception as exc:
                self._last_refresh_status[(country, range_key)] = {
                    "published": False,
                    "provider_status": {
                        "publication": {
                            "status": "refresh_failed",
                            "error": exc.__class__.__name__,
                        }
                    },
                }
                print(
                    "[EBB:charts] "
                    f"country={country} range={range_key} "
                    f"outcome=refresh_failed error={exc.__class__.__name__}"
                )
            finally:
                with self._refresh_lock:
                    self._refreshing.discard(key)

        Thread(target=run, name=f"charts-{country}-{range_key}", daemon=True).start()
        return True

    def ensure_demand_refreshes(self, *, max_regions: int = 4) -> int:
        """Prewarm Global plus recently requested charts without crawling every region."""

        targets = [("GLOBAL", "week")]
        targets.extend(
            self._repository.recent_demand(
                since=time.time() - (14 * 24 * 60 * 60),
                limit=max(max_regions - 1, 1),
            )
        )
        scheduled = 0
        for country, range_key in dict.fromkeys(targets):
            scheduled += int(
                self.ensure_refresh(country=country, range_key=range_key)
            )
        return scheduled

    def get(
        self,
        *,
        country: str = "GLOBAL",
        range_key: str = "week",
        source: str = "all",
        limit: int = 30,
    ) -> Dict[str, Any]:
        country = _country_key(country)
        range_key = _range_key(range_key)
        source = _source_key(source)
        safe_limit = max(1, min(int(limit), 60))
        demand_key = (country, range_key)
        now = time.time()
        if now - self._demand_recorded_at.get(demand_key, 0.0) >= 5 * 60:
            self._repository.record_demand(country, range_key, at=now)
            self._demand_recorded_at[demand_key] = now
        snapshot = self._repository.latest(country, range_key)
        stale = not snapshot or snapshot["expires_at"] <= time.time()
        if stale:
            self.ensure_refresh(country=country, range_key=range_key)
        key = (country, range_key)
        with self._refresh_lock:
            refreshing = key in self._refreshing
        if not snapshot:
            last_status = self._last_refresh_status.get((country, range_key), {})
            return {
                "status": (
                    "unavailable"
                    if last_status and last_status.get("published") is False
                    else "warming"
                ),
                "country": country,
                "range": range_key,
                "source": source,
                "generated_at": 0,
                "provider_status": last_status.get("provider_status") or {},
                "refreshing": refreshing,
                "entries": [],
            }
        entries = [
            dict(item)
            for item in snapshot["entries"]
            if source == "all" or source in (item.get("chart_providers") or [])
        ]
        if source != "all":
            entries.sort(
                key=lambda item: _int(
                    (item.get("provider_ranks") or {}).get(source)
                )
                or 10_000
            )
        entries = entries[:safe_limit]
        previous = self._repository.previous_ranks(
            country=country,
            range_key=range_key,
            before=snapshot["generated_at"],
            source=source,
        )
        has_previous_snapshot = self._repository.has_snapshot_before(
            country=country,
            range_key=range_key,
            before=snapshot["generated_at"],
        )
        history = self._repository.history(
            country=country,
            range_key=range_key,
            entity_keys=[item.get("entity_key") for item in entries],
            source=source,
        )
        for rank, item in enumerate(entries, start=1):
            previous_rank = previous.get(item["entity_key"])
            current_rank = (
                rank
                if source == "all"
                else _int((item.get("provider_ranks") or {}).get(source)) or rank
            )
            item["rank"] = current_rank
            item["previous_rank"] = previous_rank
            item["rank_change"] = (
                previous_rank - current_rank if previous_rank is not None else None
            )
            item["is_new_entry"] = bool(
                has_previous_snapshot and previous_rank is None
            )
            item["history"] = history.get(item["entity_key"], [])
        return {
            "status": "stale" if stale else "ready",
            "snapshot_id": snapshot["snapshot_id"],
            "country": country,
            "range": range_key,
            "source": source,
            "generated_at": snapshot["generated_at"],
            "provider_status": snapshot["provider_status"],
            "refreshing": refreshing,
            "total_count": len([
                item
                for item in snapshot["entries"]
                if source == "all" or source in (item.get("chart_providers") or [])
            ]),
            "entries": entries,
        }


_services: Dict[int, ChartsService] = {}
_services_lock = Lock()


def get_charts_service(server: Any) -> ChartsService:
    key = id(server)
    with _services_lock:
        service = _services.get(key)
        if service is None:
            service = ChartsService(server)
            _services[key] = service
        return service
