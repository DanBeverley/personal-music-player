from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List
import os
import pathlib
import re
import unicodedata

import requests


LISTENBRAINZ_BASE_URL = "https://api.listenbrainz.org/1"
LASTFM_BASE_URL = "https://ws.audioscrobbler.com/2.0/"


def configured_provider_value(name: str) -> str:
    direct = _text(os.environ.get(name))
    if direct:
        return direct
    for parent in pathlib.Path(__file__).resolve().parents:
        path = parent / ".env"
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    return ""


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _identity(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value).casefold())
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", normalized).split())


def _duration_seconds(value: Any) -> int:
    if isinstance(value, (int, float)):
        numeric = int(value)
        return numeric // 1000 if numeric > 10000 else max(numeric, 0)
    value = _text(value)
    if not value:
        return 0
    if value.isdigit():
        return _duration_seconds(int(value))
    if value.startswith("PT"):
        match = re.fullmatch(
            r"PT(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?",
            value,
        )
        if match:
            return (
                int(match.group("h") or 0) * 3600
                + int(match.group("m") or 0) * 60
                + int(match.group("s") or 0)
            )
    if ":" in value:
        try:
            total = 0
            for part in value.split(":"):
                total = total * 60 + int(part)
            return total
        except ValueError:
            return 0
    return 0


def _artist(item: Dict[str, Any]) -> str:
    value = item.get("artist") or item.get("channel") or item.get("author")
    if isinstance(value, dict):
        value = value.get("name") or value.get("title")
    if not value:
        for entry in item.get("artists") or []:
            if isinstance(entry, dict):
                value = entry.get("name") or entry.get("title")
            elif entry:
                value = entry
            if value:
                break
    return _text(value)


@dataclass(frozen=True)
class CanonicalRecording:
    title: str
    artist: str
    recording_mbid: str = ""
    artist_mbid: str = ""
    release_group_mbid: str = ""
    album: str = ""
    isrc: str = ""
    duration_seconds: int = 0

    @classmethod
    def from_item(cls, item: Dict[str, Any]) -> "CanonicalRecording":
        artist_ids = item.get("musicbrainz_artist_ids") or []
        if isinstance(artist_ids, str):
            artist_ids = [artist_ids]
        isrcs = item.get("isrcs") or []
        if isinstance(isrcs, str):
            isrcs = [isrcs]
        return cls(
            title=_text(item.get("title") or item.get("recording_name")),
            artist=_artist(item) or _text(item.get("artist_name")),
            recording_mbid=_text(
                item.get("musicbrainz_recording_id") or item.get("recording_mbid")
            ),
            artist_mbid=_text(
                item.get("musicbrainz_artist_id")
                or next(iter(artist_ids), "")
            ),
            release_group_mbid=_text(
                item.get("musicbrainz_release_group_id")
                or item.get("release_group_mbid")
            ),
            album=_text(item.get("album") or item.get("release_name")),
            isrc=_text(item.get("isrc") or next(iter(isrcs), "")),
            duration_seconds=_duration_seconds(
                item.get("duration") or item.get("duration_seconds") or item.get("length")
            ),
        )

    @property
    def semantic_key(self) -> str:
        title = _identity(self.title)
        artist = _identity(self.artist)
        return f"{title}|{artist}" if title and artist else ""

    @property
    def entity_key(self) -> str:
        if self.recording_mbid:
            return f"musicbrainz:recording:{self.recording_mbid.casefold()}"
        normalized_isrc = re.sub(r"[^A-Z0-9]", "", self.isrc.upper())
        if normalized_isrc:
            return f"isrc:{normalized_isrc}"
        return self.semantic_key

    @property
    def track_key(self) -> str:
        if self.recording_mbid:
            return f"recording:{self.recording_mbid.casefold()}"
        return f"recording:{self.entity_key}"


class JsonHttpClient:
    def __init__(self, server: Any = None, *, timeout_seconds: float = 10.0) -> None:
        self.session = getattr(server, "upstream_http", None) or requests.Session()
        self.timeout_seconds = max(float(timeout_seconds), 1.0)

    def get(self, url: str, *, params: Dict[str, Any] | None = None) -> Any:
        response = self.session.get(url, params=params, timeout=self.timeout_seconds)
        response.raise_for_status()
        return response.json()


class ListenBrainzClient(JsonHttpClient):
    def sitewide_recordings(self, *, limit: int = 40, offset: int = 0) -> List[Dict[str, Any]]:
        payload = self.get(
            f"{LISTENBRAINZ_BASE_URL}/stats/sitewide/recordings",
            params={
                "count": max(1, min(max(int(limit), 1) * 4, 100)),
                "offset": max(int(offset), 0),
                "range": "all_time",
            },
        )
        rows = (payload.get("payload") or {}).get("recordings") or []
        output: List[Dict[str, Any]] = []
        artist_counts: Dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            artist_key = _identity(row.get("artist_name"))
            if artist_key and artist_counts.get(artist_key, 0) >= 2:
                continue
            if artist_key:
                artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
            artist_ids = row.get("artist_mbids") or []
            output.append(
                {
                    "title": row.get("track_name"),
                    "artist": row.get("artist_name"),
                    "album": row.get("release_name"),
                    "musicbrainz_recording_id": row.get("recording_mbid"),
                    "musicbrainz_artist_ids": artist_ids,
                    "musicbrainz_artist_id": next(iter(artist_ids), ""),
                    "musicbrainz_release_id": row.get("release_mbid"),
                    "listen_count": row.get("listen_count"),
                    "relationship_provider": "listenbrainz",
                    "relationship_evidence": "sitewide_popularity",
                }
            )
            if len(output) >= max(int(limit), 1):
                break
        return output

    def top_recordings(self, artist_mbid: str, *, limit: int = 30) -> List[Dict[str, Any]]:
        if not _text(artist_mbid):
            return []
        try:
            payload = self.get(
                f"{LISTENBRAINZ_BASE_URL}/popularity/top-recordings-for-artist/{artist_mbid}"
            )
        except Exception:
            # ListenBrainz's popularity endpoint can return 500 even for
            # well-known valid MBIDs.  LB Radio is another structured MBID
            # endpoint and supplies canonical recordings without reverting to
            # artist-name searches.
            radio_rows = self.artist_radio(artist_mbid, limit=limit)
            metadata = self.recording_metadata(
                [row.get("recording_mbid") for row in radio_rows]
            )
            return [
                {
                    "title": (metadata.get(_text(row.get("recording_mbid"))) or {}).get("title"),
                    "album": (metadata.get(_text(row.get("recording_mbid"))) or {}).get("album"),
                    "duration": (metadata.get(_text(row.get("recording_mbid"))) or {}).get("duration"),
                    "musicbrainz_recording_id": row.get("recording_mbid"),
                    "musicbrainz_artist_id": (
                        metadata.get(_text(row.get("recording_mbid"))) or {}
                    ).get("musicbrainz_artist_id") or row.get("similar_artist_mbid") or artist_mbid,
                    "musicbrainz_release_id": (
                        metadata.get(_text(row.get("recording_mbid"))) or {}
                    ).get("musicbrainz_release_id"),
                    "musicbrainz_release_group_id": (
                        metadata.get(_text(row.get("recording_mbid"))) or {}
                    ).get("musicbrainz_release_group_id"),
                    "isrc": (metadata.get(_text(row.get("recording_mbid"))) or {}).get("isrc"),
                    "artist": (metadata.get(_text(row.get("recording_mbid"))) or {}).get("artist")
                    or row.get("similar_artist_name"),
                    "listen_count": row.get("total_listen_count"),
                    "relationship_provider": "listenbrainz",
                    "relationship_evidence": "artist_radio_catalog",
                }
                for row in radio_rows
                if _text(row.get("recording_mbid"))
            ]
        rows = payload if isinstance(payload, list) else payload.get("recordings") or []
        output: List[Dict[str, Any]] = []
        for row in rows[: max(int(limit), 1)]:
            if not isinstance(row, dict):
                continue
            output.append(
                {
                    "title": row.get("recording_name"),
                    "artist": row.get("artist_name"),
                    "album": row.get("release_name"),
                    "duration": _duration_seconds(row.get("length")),
                    "musicbrainz_recording_id": row.get("recording_mbid"),
                    "musicbrainz_artist_ids": row.get("artist_mbids") or [],
                    "musicbrainz_release_id": row.get("release_mbid"),
                    "listen_count": row.get("total_listen_count"),
                    "listener_count": row.get("total_user_count"),
                    "relationship_provider": "listenbrainz",
                    "relationship_evidence": "artist_popularity",
                }
            )
        return output

    def top_release_groups(self, artist_mbid: str, *, limit: int = 12) -> List[Dict[str, Any]]:
        if not _text(artist_mbid):
            return []
        payload = self.get(
            f"{LISTENBRAINZ_BASE_URL}/popularity/top-release-groups-for-artist/{artist_mbid}"
        )
        rows = payload if isinstance(payload, list) else payload.get("release_groups") or []
        output: List[Dict[str, Any]] = []
        for row in rows[: max(int(limit), 1)]:
            if not isinstance(row, dict):
                continue
            release_group = row.get("release_group") or row.get("release") or {}
            artist = row.get("artist") or {}
            output.append(
                {
                    "id": f"musicbrainz:release-group:{row.get('release_group_mbid')}",
                    "title": release_group.get("name"),
                    "artist": artist.get("name"),
                    "musicbrainz_release_group_id": row.get("release_group_mbid"),
                    "musicbrainz_artist_id": artist_mbid,
                    "release_date": release_group.get("date"),
                    "release_type": release_group.get("type"),
                    "listen_count": row.get("total_listen_count"),
                    "listener_count": row.get("total_user_count"),
                    "relationship_provider": "listenbrainz",
                    "relationship_evidence": "artist_release_popularity",
                }
            )
        return output

    def artist_radio(self, artist_mbid: str, *, limit: int = 40) -> List[Dict[str, Any]]:
        if not _text(artist_mbid):
            return []
        payload = self.get(
            f"{LISTENBRAINZ_BASE_URL}/lb-radio/artist/{artist_mbid}",
            params={
                "mode": "medium",
                "max_similar_artists": 8,
                "max_recordings_per_artist": 8,
                "pop_begin": 0,
                "pop_end": 100,
                "count": max(int(limit), 1),
            },
        )
        rows: List[Any] = []
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            for value in payload.values():
                if isinstance(value, list):
                    rows.extend(value)
        return [dict(row) for row in rows[: max(int(limit), 1)] if isinstance(row, dict)]

    def recording_metadata(self, recording_mbids: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
        mbids = list(dict.fromkeys(_text(value) for value in recording_mbids if _text(value)))
        if not mbids:
            return {}
        output: Dict[str, Dict[str, Any]] = {}
        for offset in range(0, len(mbids), 50):
            batch = mbids[offset : offset + 50]
            payload = self.get(
                f"{LISTENBRAINZ_BASE_URL}/metadata/recording/",
                params={
                    "recording_mbids": ",".join(batch),
                    "inc": "artist release",
                },
            )
            if not isinstance(payload, dict):
                continue
            for mbid, raw in payload.items():
                if not isinstance(raw, dict):
                    continue
                recording = raw.get("recording") or {}
                artist = raw.get("artist") or {}
                release = raw.get("release") or {}
                artists = artist.get("artists") or []
                first_artist = next(
                    (entry for entry in artists if isinstance(entry, dict)),
                    {},
                )
                isrcs = recording.get("isrcs") or []
                output[_text(mbid)] = {
                    "title": recording.get("name"),
                    "artist": artist.get("name") or first_artist.get("name"),
                    "album": release.get("name"),
                    "duration": _duration_seconds(recording.get("length")),
                    "isrc": next(iter(isrcs), ""),
                    "musicbrainz_artist_id": first_artist.get("artist_mbid"),
                    "musicbrainz_release_id": release.get("mbid"),
                    "musicbrainz_release_group_id": release.get("release_group_mbid"),
                }
        return output


class LastFmClient(JsonHttpClient):
    def __init__(
        self,
        server: Any = None,
        *,
        api_key: str = "",
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(server, timeout_seconds=timeout_seconds)
        self.api_key = _text(api_key or configured_provider_value("LASTFM_API_KEY"))

    def _call(self, method: str, **params: Any) -> Dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("lastfm_api_key_missing")
        payload = self.get(
            LASTFM_BASE_URL,
            params={
                "method": method,
                "api_key": self.api_key,
                "format": "json",
                "autocorrect": 1,
                **params,
            },
        )
        if not isinstance(payload, dict):
            return {}
        if payload.get("error"):
            raise RuntimeError(f"lastfm_{payload.get('error')}:{payload.get('message', '')}")
        return payload

    def similar_tracks(self, recording: CanonicalRecording, *, limit: int = 30) -> List[Dict[str, Any]]:
        result_limit = max(int(limit), 1)
        params: Dict[str, Any] = {"limit": result_limit}
        if recording.recording_mbid:
            params["mbid"] = recording.recording_mbid
        else:
            params.update({"track": recording.title, "artist": recording.artist})
        try:
            payload = self._call("track.getsimilar", **params)
        except RuntimeError as exc:
            if not recording.recording_mbid or not str(exc).startswith("lastfm_6:"):
                raise
            payload = self._call(
                "track.getsimilar",
                track=recording.title,
                artist=recording.artist,
                limit=result_limit,
            )
        rows = (payload.get("similartracks") or {}).get("track") or []
        if not rows and recording.recording_mbid:
            payload = self._call(
                "track.getsimilar",
                track=recording.title,
                artist=recording.artist,
                limit=result_limit,
            )
            rows = (payload.get("similartracks") or {}).get("track") or []
        output: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            artist = row.get("artist") or {}
            output.append(
                {
                    "title": row.get("name"),
                    "artist": artist.get("name") if isinstance(artist, dict) else artist,
                    "musicbrainz_recording_id": row.get("mbid"),
                    "relationship_score": float(row.get("match") or 0.0),
                    "related_to_track": recording.track_key,
                    "relationship_provider": "lastfm",
                    "relationship_evidence": "track_similarity",
                }
            )
        return output

    def similar_artists(self, artist: str, *, artist_mbid: str = "", limit: int = 16) -> List[Dict[str, Any]]:
        result_limit = max(int(limit), 1)
        params: Dict[str, Any] = {"limit": result_limit}
        if artist_mbid:
            params["mbid"] = artist_mbid
        else:
            params["artist"] = artist
        try:
            payload = self._call("artist.getsimilar", **params)
        except RuntimeError as exc:
            if not artist_mbid or not str(exc).startswith("lastfm_6:"):
                raise
            payload = self._call("artist.getsimilar", artist=artist, limit=result_limit)
        rows = (payload.get("similarartists") or {}).get("artist") or []
        if not rows and artist_mbid:
            payload = self._call("artist.getsimilar", artist=artist, limit=result_limit)
            rows = (payload.get("similarArtists") or payload.get("similarartists") or {}).get("artist") or []
        return [
            {
                "id": f"musicbrainz:artist:{row.get('mbid')}",
                "name": row.get("name"),
                "artist": row.get("name"),
                "musicbrainz_artist_id": row.get("mbid"),
                "relationship_score": float(row.get("match") or 0.0),
                "related_to_artist": artist,
                "relationship_provider": "lastfm",
                "relationship_evidence": "artist_similarity",
                "source_authority": "verified_catalog",
            }
            for row in rows
            if isinstance(row, dict) and _text(row.get("name"))
        ]

    def tag_tracks(self, tag: str, *, limit: int = 30) -> List[Dict[str, Any]]:
        payload = self._call("tag.gettoptracks", tag=tag, limit=max(int(limit), 1))
        rows = (payload.get("tracks") or {}).get("track") or []
        return [
            {
                "title": row.get("name"),
                "artist": (row.get("artist") or {}).get("name")
                if isinstance(row.get("artist"), dict)
                else row.get("artist"),
                "musicbrainz_recording_id": row.get("mbid"),
                "tags": [tag],
                "genre": tag,
                "relationship_provider": "lastfm",
                "relationship_evidence": "structured_tag",
            }
            for row in rows
            if isinstance(row, dict)
        ]
