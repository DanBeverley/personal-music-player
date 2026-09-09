from __future__ import annotations

import json
import re
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Thread
import time
import unicodedata
import uuid
from typing import Any, Dict, List

from ..discovery.structured_providers import ListenBrainzClient
from ..recommend.store_runtime import open_recommendation_store_connection


RELEASE_REFRESH_SECONDS = 30 * 60
RELEASE_PUBLISH_LIMIT = 120
RELEASE_WINDOW_DAYS = 90


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _popularity(value: Any) -> float:
    match = re.search(r"([\d,.]+)\s*([KMB]?)", _text(value).upper())
    if not match:
        return -1
    try:
        return float(match[1].replace(",", "")) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[match[2]]
    except ValueError:
        return -1


def _identity(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value).casefold())
    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", normalized).split())


def _artist(item: Dict[str, Any]) -> str:
    direct = _text(item.get("artist") or item.get("artist_name"))
    if direct:
        return direct
    return ", ".join(
        _text(value.get("name"))
        for value in item.get("artists") or []
        if isinstance(value, dict) and _text(value.get("name"))
    )


def _thumbnail(item: Dict[str, Any]) -> str:
    values = [
        _text(value.get("url") or value.get("#text"))
        for value in item.get("thumbnails") or []
        if isinstance(value, dict) and _text(value.get("url") or value.get("#text"))
    ]
    return _text(item.get("thumbnail")) or (values[-1] if values else "")


def _key(title: Any, artist: Any) -> str:
    normalized_title = _identity(title)
    normalized_artist = _identity(artist)
    return f"{normalized_title}|{normalized_artist}" if normalized_title else ""


class NewReleasesRepository:
    def __init__(self, server: Any) -> None:
        self._server = server
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS new_release_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    generated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    provider_status_json TEXT NOT NULL,
                    entries_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_new_release_snapshots_latest
                    ON new_release_snapshots(generated_at DESC);
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _connect(self):
        return open_recommendation_store_connection(self._server)

    def latest(self) -> Dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM new_release_snapshots ORDER BY generated_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            return {
                "snapshot_id": row["snapshot_id"],
                "generated_at": float(row["generated_at"]),
                "expires_at": float(row["expires_at"]),
                "provider_status": json.loads(row["provider_status_json"] or "{}"),
                "entries": json.loads(row["entries_json"] or "[]"),
            }
        finally:
            connection.close()

    def publish(
        self,
        entries: List[Dict[str, Any]],
        provider_status: Dict[str, Any],
    ) -> str:
        now = time.time()
        snapshot_id = str(uuid.uuid4())
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO new_release_snapshots(
                    snapshot_id, generated_at, expires_at,
                    provider_status_json, entries_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    snapshot_id,
                    now,
                    now + RELEASE_REFRESH_SECONDS,
                    json.dumps(provider_status, ensure_ascii=False),
                    json.dumps(entries, ensure_ascii=False),
                ],
            )
            connection.execute(
                """
                DELETE FROM new_release_snapshots
                WHERE snapshot_id NOT IN (
                    SELECT snapshot_id FROM new_release_snapshots
                    ORDER BY generated_at DESC LIMIT 48
                )
                """
            )
            connection.commit()
            return snapshot_id
        finally:
            connection.close()


class NewReleasesService:
    def __init__(self, server: Any) -> None:
        self._server = server
        self._repository = NewReleasesRepository(server)
        self._refresh_lock = Lock()
        self._retry_after = 0.0
        self._last_refresh: Dict[str, Any] = {}

    def _ytmusic(self) -> List[Dict[str, Any]]:
        payload = self._server.ytmusic.get_explore() or {}
        return [
            dict(value)
            for value in payload.get("new_releases") or []
            if isinstance(value, dict)
        ]

    def _resolve_fresh_releases(
        self,
        rows: List[Dict[str, Any]],
        *,
        limit: int = 60,
    ) -> List[Dict[str, Any]]:
        """Bind canonical fresh-release evidence to detail-ready YT albums."""

        cutoff = (date.today() - timedelta(days=RELEASE_WINDOW_DAYS)).isoformat()
        candidates = sorted(
            [row for row in rows if cutoff <= _text(row.get("release_date")) <= date.today().isoformat()],
            key=lambda value: (
                _text(value.get("release_date")),
                value.get("release_group_primary_type") == "Album",
            ),
            reverse=True,
        )
        # Spread the bounded lookup budget across months instead of spending
        # every lookup on today's releases.
        months: Dict[str, list] = {}
        for row in candidates:
            months.setdefault(_text(row.get("release_date"))[:7], []).append(row)
        candidates = [
            values[index]
            for index in range(max((len(values) for values in months.values()), default=0))
            for values in months.values() if index < len(values)
        ]
        unique: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for value in candidates:
            identity = _key(
                value.get("release_name"),
                value.get("artist_credit_name"),
            )
            if not identity or identity in seen:
                continue
            seen.add(identity)
            unique.append(value)
            if len(unique) >= max(int(limit), 1):
                break

        def resolve(value: Dict[str, Any]) -> Dict[str, Any] | None:
            title = _text(value.get("release_name"))
            artist = _text(value.get("artist_credit_name"))
            results = self._server.ytmusic.search(
                f"{title} {artist}",
                filter="albums",
                limit=4,
            ) or []
            title_key = _identity(title)
            artist_key = _identity(artist)
            for raw in results:
                if not isinstance(raw, dict):
                    continue
                if _identity(raw.get("title")) != title_key:
                    continue
                resolved_artist = _identity(_artist(raw))
                if artist_key and resolved_artist and (
                    artist_key not in resolved_artist
                    and resolved_artist not in artist_key
                ):
                    continue
                return dict(raw)
            return None

        resolved: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(resolve, value): value for value in unique
            }
            for future in as_completed(futures):
                try:
                    item = future.result()
                except Exception:
                    continue
                if item:
                    resolved.append(item)
        resolved.sort(
            key=lambda value: next(
                (
                    index
                    for index, source in enumerate(unique)
                    if _key(value.get("title"), _artist(value))
                    == _key(
                        source.get("release_name"),
                        source.get("artist_credit_name"),
                    )
                ),
                len(unique),
            )
        )
        return resolved

    def refresh(self) -> Dict[str, Any]:
        # Publish the smaller successful window before attempting the expensive
        # extension. A timeout must not discard usable acquisition work.
        recent = self._refresh_window(days=45)
        extended = self._refresh_window(days=RELEASE_WINDOW_DAYS)
        result = dict(extended if extended.get("published") else recent)
        result["refresh_status"] = (
            "complete" if extended.get("published") and extended.get("provider_status", {}).get("listenbrainz", {}).get("status") == "ok" else
            "partial" if recent.get("published") or extended.get("published") else "failed"
        )
        result["attempted_provider_status"] = {
            "recent": recent.get("provider_status", {}),
            "extended": extended.get("provider_status", {}),
        }
        return result

    def _refresh_window(self, *, days: int) -> Dict[str, Any]:
        provider_status: Dict[str, Any] = {}
        try:
            ytmusic = self._ytmusic()
            provider_status["ytmusic"] = {"status": "ok" if ytmusic else "shelf_unavailable", "count": len(ytmusic)}
        except Exception as exc:
            ytmusic = []
            provider_status["ytmusic"] = {
                "status": "failed",
                "error": exc.__class__.__name__,
            }
        try:
            listenbrainz = ListenBrainzClient(self._server).fresh_releases(days=days)
            provider_status["listenbrainz"] = {
                "status": "ok",
                "count": len(listenbrainz),
                "window_days": days,
            }
        except Exception as exc:
            listenbrainz = []
            provider_status["listenbrainz"] = {
                "status": "failed",
                "error": exc.__class__.__name__,
            }

        if listenbrainz:
            try:
                existing = {_key(item.get("title"), _artist(item)) for item in ytmusic}
                ytmusic.extend(self._resolve_fresh_releases([
                    item for item in listenbrainz
                    if _key(item.get("release_name"), item.get("artist_credit_name")) not in existing
                ]))
                provider_status["ytmusic"] = {
                    "status": "ok",
                    "count": len(ytmusic),
                    "mode": "canonical_fresh_release_resolution",
                }
            except Exception as exc:
                provider_status["ytmusic"] = {
                    "status": "failed",
                    "error": exc.__class__.__name__,
                }

        previous = self._repository.latest()
        previous_entries = (previous or {}).get("entries", [])
        prepared_albums = {entry["id"]: entry for entry in previous_entries
                           if entry.get("entry_type") == "album" and entry.get("detail_ready")}

        canonical = {
            _key(value.get("release_name"), value.get("artist_credit_name")): value
            for value in listenbrainz
            if _key(value.get("release_name"), value.get("artist_credit_name"))
        }
        entries: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for rank, raw in enumerate(ytmusic, start=1):
            title = _text(raw.get("title"))
            artist = _artist(raw)
            browse_id = _text(raw.get("browseId") or raw.get("id"))
            identity = _key(title, artist)
            if not title or not artist or not browse_id or identity in seen:
                continue
            seen.add(identity)
            evidence = canonical.get(identity) or {}
            release_date = _text(evidence.get("release_date"))
            if release_date and not (
                (date.today() - timedelta(days=RELEASE_WINDOW_DAYS)).isoformat()
                <= release_date <= date.today().isoformat()
            ):
                continue
            release_mbid = _text(evidence.get("release_mbid"))
            release_group_mbid = _text(evidence.get("release_group_mbid"))
            entries.append(
                {
                    **raw,
                    "id": browse_id,
                    "browseId": browse_id,
                    "title": title,
                    "name": title,
                    "artist": artist,
                    "thumbnail": _thumbnail(raw),
                    "release_date": release_date,
                    "year": _text(raw.get("year")) or release_date[:4],
                    "musicbrainz_release_id": release_mbid,
                    "musicbrainz_release_group_id": release_group_mbid,
                    "release_rank": rank,
                    "release_providers": [
                        "ytmusic",
                        *(["listenbrainz"] if evidence else []),
                    ],
                    "entry_type": "album",
                }
            )
            if len(entries) >= RELEASE_PUBLISH_LIMIT:
                break
        from ..details.detail_runtime import build_album_details_payload

        def hydrate(entry: Dict[str, Any]) -> list[Dict[str, Any]]:
            if entry["id"] in prepared_albums:
                retained = [track for track in previous_entries
                            if track.get("entry_type") == "track" and track.get("album_id") == entry["id"]]
                if retained:
                    return [{**prepared_albums[entry["id"]], **entry}, *retained]
            try:
                detail = build_album_details_payload(self._server, entry["id"])
            except Exception as exc:
                print(f"[EBB:new-releases] detail_error={type(exc).__name__} album={entry['id']}")
                return []
            tracks = [track for track in detail.get("tracks") or [] if track.get("id")]
            if not tracks:
                return []
            album = {**entry, "detail_ready": True, "views": detail.get("views")}
            output = [album]
            # Keep a varied track chart, rather than letting one album take it over.
            for track in tracks[:3]:
                output.append({
                    **track,
                    "entry_type": "track",
                    "artist": track.get("channel") or entry["artist"],
                    "album_id": entry["id"],
                    "release_date": entry["release_date"],
                    "release_providers": entry["release_providers"],
                })
            return output

        with ThreadPoolExecutor(max_workers=4) as executor:
            hydrated = list(executor.map(hydrate, entries))
        unique_entries = {}
        for group in hydrated:
            for entry in group:
                unique_entries.setdefault((entry["entry_type"], entry["id"]), entry)
        entries = list(unique_entries.values())
        if not entries:
            return {"published": False, "count": 0, "provider_status": provider_status}
        cutoff = (date.today() - timedelta(days=RELEASE_WINDOW_DAYS)).isoformat()
        for entry in previous_entries:
            if entry.get("entry_type") and cutoff <= _text(entry.get("release_date")) <= date.today().isoformat():
                unique_entries.setdefault((entry["entry_type"], entry["id"]), entry)
        entries = list(unique_entries.values())
        for kind in ("track", "album"):
            ranked = sorted(
                (entry for entry in entries if entry["entry_type"] == kind),
                key=lambda entry: _popularity(entry.get("views")), reverse=True,
            )
            for rank, entry in enumerate(ranked, 1):
                entry["release_rank"] = rank
        entries.sort(key=lambda entry: (entry["entry_type"], entry["release_rank"]))
        if not entries:
            return {
                "published": False,
                "count": 0,
                "provider_status": provider_status,
            }
        snapshot_id = self._repository.publish(entries, provider_status)
        return {
            "published": True,
            "snapshot_id": snapshot_id,
            "count": len(entries),
            "provider_status": provider_status,
        }

    def get(self, *, limit: int = 30, force: bool = False, background: bool = False) -> Dict[str, Any]:
        snapshot = self._repository.latest()
        needs_refresh = force or not snapshot or snapshot["expires_at"] <= time.time() or not any(
            "entry_type" in entry for entry in snapshot["entries"]
        )
        if needs_refresh and (force or time.time() >= self._retry_after):
            if self._refresh_lock.acquire(blocking=False):
                def refresh_snapshot():
                    try:
                        result = self.refresh()
                        self._last_refresh = {**result, "attempted_at": time.time()}
                        self._retry_after = 0 if result.get("published") else time.time() + 30
                    except Exception as exc:
                        self._last_refresh = {"refresh_status": "failed", "attempted_at": time.time(), "error": type(exc).__name__}
                        self._retry_after = time.time() + 30
                        print(f"[EBB:new-releases] refresh_failed={type(exc).__name__}")
                    finally:
                        self._refresh_lock.release()
                if background:
                    Thread(target=refresh_snapshot, daemon=True, name="new-releases").start()
                else:
                    refresh_snapshot()
                snapshot = self._repository.latest()
        if not snapshot:
            return {
                "status": "preparing" if self._refresh_lock.locked() else "unavailable",
                "generated_at": 0,
                "provider_status": {},
                "entries": [],
                "refresh": self._last_refresh,
            }
        return {
            "status": "ready" if snapshot["expires_at"] > time.time() else "stale",
            "refreshing": self._refresh_lock.locked(),
            "snapshot_id": snapshot["snapshot_id"],
            "generated_at": snapshot["generated_at"],
            "provider_status": snapshot["provider_status"],
            "refresh": self._last_refresh,
            "entries": [
                entry for kind in ("track", "album")
                for entry in [value for value in snapshot["entries"] if value.get("entry_type", "album") == kind]
                [: max(1, min(int(limit), RELEASE_PUBLISH_LIMIT))]
            ],
        }


_services: Dict[int, NewReleasesService] = {}
_services_lock = Lock()


def get_new_releases_service(server: Any) -> NewReleasesService:
    key = id(server)
    with _services_lock:
        service = _services.get(key)
        if service is None:
            service = NewReleasesService(server)
            _services[key] = service
        return service
