from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List


MUSICBRAINZ_BASE_URL = os.environ.get(
    "AURALIS_MUSICBRAINZ_BASE_URL",
    "https://musicbrainz.org/ws/2",
).rstrip("/")
MUSICBRAINZ_USER_AGENT = os.environ.get(
    "AURALIS_MUSICBRAINZ_USER_AGENT",
    "Neatie/1.0 (hoap43431@gmail.com)",
).strip()
MUSICBRAINZ_TIMEOUT_SECONDS = max(
    0.5,
    float(os.environ.get("AURALIS_MUSICBRAINZ_TIMEOUT_SECONDS", "1.8")),
)
MUSICBRAINZ_MIN_INTERVAL_SECONDS = max(
    1.0,
    float(os.environ.get("AURALIS_MUSICBRAINZ_MIN_INTERVAL_SECONDS", "1.05")),
)

_rate_lock = threading.Lock()
_last_request_at = 0.0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first_text(values: Iterable[Any]) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _artist_credit_name(artist_credit: Any) -> str:
    if not isinstance(artist_credit, list):
        return ""
    parts: List[str] = []
    for credit in artist_credit:
        if not isinstance(credit, dict):
            continue
        name = _text(credit.get("name"))
        if not name and isinstance(credit.get("artist"), dict):
            name = _text(credit["artist"].get("name"))
        joinphrase = _text(credit.get("joinphrase"))
        if name:
            parts.append(name + joinphrase)
    return "".join(parts).strip()


def _artist_credit_ids(artist_credit: Any) -> List[str]:
    ids: List[str] = []
    if not isinstance(artist_credit, list):
        return ids
    for credit in artist_credit:
        artist = credit.get("artist") if isinstance(credit, dict) else None
        if isinstance(artist, dict):
            artist_id = _text(artist.get("id"))
            if artist_id and artist_id not in ids:
                ids.append(artist_id)
    return ids


def _recording_release_fields(recording: Dict[str, Any]) -> Dict[str, str]:
    releases = recording.get("releases")
    if not isinstance(releases, list) or not releases:
        return {
            "album": "",
            "release_id": "",
            "release_group_id": "",
            "release_date": "",
            "release_year": "",
            "country": "",
        }
    # Prefer earliest official-looking release. MusicBrainz search commonly
    # returns multiple releases for one recording.
    release = sorted(
        [item for item in releases if isinstance(item, dict)],
        key=lambda item: (
            _text(item.get("date")) or "9999",
            _text(item.get("title")),
        ),
    )[0]
    release_group = release.get("release-group")
    release_group_id = (
        _text(release_group.get("id")) if isinstance(release_group, dict) else ""
    )
    release_date = _text(release.get("date"))
    return {
        "album": _text(release.get("title")),
        "release_id": _text(release.get("id")),
        "release_group_id": release_group_id,
        "release_date": release_date,
        "release_year": release_date[:4] if len(release_date) >= 4 else "",
        "country": _text(release.get("country")),
    }


def _aliases_from_payload(payload: Dict[str, Any], *, query: str = "") -> List[str]:
    aliases: List[str] = []

    def add(value: Any) -> None:
        text = _text(value)
        if text and text not in aliases:
            aliases.append(text)

    add(query)
    for key in ("name", "title", "sort-name", "disambiguation"):
        add(payload.get(key))
    for alias in payload.get("aliases") or []:
        if isinstance(alias, dict):
            add(alias.get("name") or alias.get("sort-name"))
    for tag in payload.get("tags") or []:
        if isinstance(tag, dict):
            add(tag.get("name"))
    return aliases


def _recording_aliases(recording: Dict[str, Any], *, query: str = "") -> List[str]:
    aliases: List[str] = []

    def add(value: Any) -> None:
        text = _text(value)
        if text and text not in aliases:
            aliases.append(text)

    title = _text(recording.get("title"))
    artist = _artist_credit_name(recording.get("artist-credit"))
    album_fields = _recording_release_fields(recording)
    add(query)
    add(title)
    if title and artist:
        add(f"{artist} {title}")
        add(f"{title} {artist}")
    if title and album_fields.get("album"):
        add(f"{title} {album_fields['album']}")
    for alias in recording.get("aliases") or []:
        if isinstance(alias, dict):
            add(alias.get("name") or alias.get("sort-name"))
    for tag in recording.get("tags") or []:
        if isinstance(tag, dict):
            add(tag.get("name"))
    return aliases


def musicbrainz_recording_to_item(
    recording: Dict[str, Any],
    *,
    query: str = "",
) -> Dict[str, Any]:
    title = _text(recording.get("title"))
    artist = _artist_credit_name(recording.get("artist-credit"))
    release = _recording_release_fields(recording)
    score = 0.0
    try:
        score = float(recording.get("score") or 0.0) / 100.0
    except (TypeError, ValueError):
        score = 0.0
    mbid = _text(recording.get("id"))
    artist_ids = _artist_credit_ids(recording.get("artist-credit"))
    aliases = _recording_aliases(recording, query=query)
    return {
        "id": f"musicbrainz:recording:{mbid}" if mbid else "",
        "title": title,
        "artist": artist,
        "channel": artist,
        "album": release["album"],
        "source": "musicbrainz",
        "source_provider": "musicbrainz",
        "source_authority": "verified_catalog",
        "source_identity_authority": "verified_catalog",
        "source_identity_confidence": 0.96,
        "musicbrainz_recording_id": mbid,
        "musicbrainz_artist_id": artist_ids[0] if artist_ids else "",
        "musicbrainz_artist_ids": artist_ids,
        "musicbrainz_release_id": release["release_id"],
        "musicbrainz_release_group_id": release["release_group_id"],
        "release_date": release["release_date"],
        "release_year": release["release_year"],
        "country": release["country"],
        "aliases": aliases,
        "popularity": max(0.0, min(score, 1.0)),
        "musicbrainz_score": score,
        "catalog_verified": True,
        "playable": False,
        "metadata_source": "musicbrainz",
        "raw_musicbrainz": {
            "id": mbid,
            "score": recording.get("score"),
            "disambiguation": recording.get("disambiguation"),
            "length": recording.get("length"),
        },
    }


def musicbrainz_artist_to_item(
    artist: Dict[str, Any],
    *,
    query: str = "",
) -> Dict[str, Any]:
    name = _text(artist.get("name"))
    mbid = _text(artist.get("id"))
    score = 0.0
    try:
        score = float(artist.get("score") or 0.0) / 100.0
    except (TypeError, ValueError):
        score = 0.0
    aliases = _aliases_from_payload(artist, query=query)
    return {
        "id": f"musicbrainz:artist:{mbid}" if mbid else "",
        "name": name,
        "title": name,
        "artist": name,
        "channel": name,
        "source": "musicbrainz",
        "source_provider": "musicbrainz",
        "source_authority": "verified_catalog",
        "source_identity_authority": "verified_catalog",
        "source_identity_confidence": 0.96,
        "musicbrainz_artist_id": mbid,
        "country": _text(artist.get("country")),
        "type": _text(artist.get("type")),
        "aliases": aliases,
        "popularity": max(0.0, min(score, 1.0)),
        "musicbrainz_score": score,
        "catalog_verified": True,
        "playable": False,
        "metadata_source": "musicbrainz",
        "raw_musicbrainz": {
            "id": mbid,
            "score": artist.get("score"),
            "disambiguation": artist.get("disambiguation"),
        },
    }


def musicbrainz_release_group_to_item(
    release_group: Dict[str, Any],
    *,
    query: str = "",
) -> Dict[str, Any]:
    title = _text(release_group.get("title"))
    artist = _artist_credit_name(release_group.get("artist-credit"))
    mbid = _text(release_group.get("id"))
    score = 0.0
    try:
        score = float(release_group.get("score") or 0.0) / 100.0
    except (TypeError, ValueError):
        score = 0.0
    first_release_date = _text(release_group.get("first-release-date"))
    aliases = _aliases_from_payload(release_group, query=query)
    if title and artist:
        for value in (f"{artist} {title}", f"{title} {artist}"):
            if value not in aliases:
                aliases.append(value)
    return {
        "id": f"musicbrainz:release-group:{mbid}" if mbid else "",
        "title": title,
        "name": title,
        "album": title,
        "artist": artist,
        "channel": artist,
        "source": "musicbrainz",
        "source_provider": "musicbrainz",
        "source_authority": "verified_catalog",
        "source_identity_authority": "verified_catalog",
        "source_identity_confidence": 0.95,
        "musicbrainz_release_group_id": mbid,
        "musicbrainz_artist_ids": _artist_credit_ids(release_group.get("artist-credit")),
        "release_date": first_release_date,
        "release_year": first_release_date[:4] if len(first_release_date) >= 4 else "",
        "release_type": _text(release_group.get("primary-type")),
        "aliases": aliases,
        "popularity": max(0.0, min(score, 1.0)),
        "musicbrainz_score": score,
        "catalog_verified": True,
        "playable": False,
        "metadata_source": "musicbrainz",
        "raw_musicbrainz": {
            "id": mbid,
            "score": release_group.get("score"),
            "disambiguation": release_group.get("disambiguation"),
        },
    }


class MusicBrainzClient:
    def __init__(
        self,
        *,
        base_url: str = MUSICBRAINZ_BASE_URL,
        user_agent: str = MUSICBRAINZ_USER_AGENT,
        timeout_seconds: float = MUSICBRAINZ_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent.strip() or MUSICBRAINZ_USER_AGENT
        self.timeout_seconds = timeout_seconds

    def _get_json(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        global _last_request_at
        with _rate_lock:
            delay = MUSICBRAINZ_MIN_INTERVAL_SECONDS - (time.time() - _last_request_at)
            if delay > 0:
                time.sleep(delay)
            _last_request_at = time.time()
        query = urllib.parse.urlencode(
            {key: value for key, value in params.items() if value not in (None, "")}
        )
        url = f"{self.base_url}/{path}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            raw = response.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def search_recordings(self, query: str, *, limit: int = 5) -> List[Dict[str, Any]]:
        normalized_query = _text(query)
        if not normalized_query:
            return []
        payload = self._get_json(
            "recording",
            {
                "query": normalized_query,
                "fmt": "json",
                "limit": max(1, min(int(limit or 1), 25)),
            },
        )
        recordings = payload.get("recordings")
        if not isinstance(recordings, list):
            return []
        return [recording for recording in recordings if isinstance(recording, dict)]

    def search_artists(self, query: str, *, limit: int = 5) -> List[Dict[str, Any]]:
        normalized_query = _text(query)
        if not normalized_query:
            return []
        payload = self._get_json(
            "artist",
            {
                "query": normalized_query,
                "fmt": "json",
                "limit": max(1, min(int(limit or 1), 25)),
            },
        )
        artists = payload.get("artists")
        if not isinstance(artists, list):
            return []
        return [artist for artist in artists if isinstance(artist, dict)]

    def search_release_groups(self, query: str, *, limit: int = 5) -> List[Dict[str, Any]]:
        normalized_query = _text(query)
        if not normalized_query:
            return []
        payload = self._get_json(
            "release-group",
            {
                "query": normalized_query,
                "fmt": "json",
                "limit": max(1, min(int(limit or 1), 25)),
            },
        )
        release_groups = payload.get("release-groups")
        if not isinstance(release_groups, list):
            return []
        return [item for item in release_groups if isinstance(item, dict)]


def search_musicbrainz_recording_items(
    query: str,
    *,
    client: MusicBrainzClient | None = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    try:
        resolved_client = client or MusicBrainzClient()
        recordings = resolved_client.search_recordings(query, limit=limit)
    except Exception:
        return []
    items = [
        musicbrainz_recording_to_item(recording, query=query)
        for recording in recordings
        if isinstance(recording, dict)
    ]
    return [
        item
        for item in items
        if _text(item.get("title")) and _text(item.get("artist"))
    ]


def search_musicbrainz_artist_items(
    query: str,
    *,
    client: MusicBrainzClient | None = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    try:
        resolved_client = client or MusicBrainzClient()
        artists = resolved_client.search_artists(query, limit=limit)
    except Exception:
        return []
    items = [
        musicbrainz_artist_to_item(artist, query=query)
        for artist in artists
        if isinstance(artist, dict)
    ]
    return [item for item in items if _text(item.get("name"))]


def search_musicbrainz_album_items(
    query: str,
    *,
    client: MusicBrainzClient | None = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    try:
        resolved_client = client or MusicBrainzClient()
        release_groups = resolved_client.search_release_groups(query, limit=limit)
    except Exception:
        return []
    items = [
        musicbrainz_release_group_to_item(release_group, query=query)
        for release_group in release_groups
        if isinstance(release_group, dict)
    ]
    return [
        item
        for item in items
        if _text(item.get("title")) and _text(item.get("artist"))
    ]
