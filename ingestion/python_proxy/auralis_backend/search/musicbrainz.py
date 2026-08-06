from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
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


def _lucene_phrase(value: Any) -> str:
    text = _text(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _identity_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value).casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w]+", " ", normalized).split())


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


def _recording_release_candidates(recording: Dict[str, Any]) -> List[Dict[str, Any]]:
    releases = recording.get("releases")
    if not isinstance(releases, list) or not releases:
        return []
    candidates: List[Dict[str, Any]] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        release_group = release.get("release-group")
        release_group = release_group if isinstance(release_group, dict) else {}
        release_date = _text(release.get("date"))
        secondary_types = [
            _text(value)
            for value in list(release_group.get("secondary-types") or [])
            if _text(value)
        ]
        candidates.append(
            {
                "album": _text(release.get("title")),
                "release_id": _text(release.get("id")),
                "release_group_id": _text(release_group.get("id")),
                "release_date": release_date,
                "release_year": release_date[:4] if len(release_date) >= 4 else "",
                "country": _text(release.get("country")),
                "status": _text(release.get("status")),
                "primary_type": _text(release_group.get("primary-type")),
                "secondary_types": secondary_types,
            }
        )
    # A recording can appear on its original album, singles, compilations and
    # later reissues. Prefer a normal official album before falling back to the
    # earliest other release. This keeps containing-album identity useful
    # without treating the first MusicBrainz row as authoritative.
    candidates.sort(
        key=lambda item: (
            _text(item.get("primary_type")).casefold() != "album",
            bool(item.get("secondary_types")),
            _text(item.get("status")).casefold() not in {"", "official"},
            _text(item.get("release_date")) or "9999",
            _text(item.get("album")),
        )
    )
    return candidates


def _recording_release_fields(recording: Dict[str, Any]) -> Dict[str, Any]:
    candidates = _recording_release_candidates(recording)
    if not candidates:
        return {
            "album": "",
            "release_id": "",
            "release_group_id": "",
            "release_date": "",
            "release_year": "",
            "country": "",
            "release_candidates": [],
        }
    selected = dict(candidates[0])
    return {
        **selected,
        "release_candidates": candidates,
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
        "musicbrainz_release_candidates": list(
            release.get("release_candidates") or []
        ),
        "release_date": release["release_date"],
        "release_year": release["release_year"],
        "country": release["country"],
        "release_status": release.get("status") or "",
        "release_primary_type": release.get("primary_type") or "",
        "release_secondary_types": list(release.get("secondary_types") or []),
        "first_release_date": _text(recording.get("first-release-date")),
        "duration": int(float(recording.get("length") or 0) / 1000.0),
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
            "video": bool(recording.get("video")),
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

    def lookup_recordings(
        self,
        recording_ids: Iterable[Any],
        *,
        limit: int = 40,
    ) -> List[Dict[str, Any]]:
        """Resolve known recording MBIDs in one MusicBrainz search request.

        Feed metadata completion already knows the canonical identities.  An
        OR query avoids the old one-request-per-track hydration pattern while
        still returning release and release-group data for each recording.
        """

        normalized_ids = list(
            dict.fromkeys(
                _text(value).casefold()
                for value in recording_ids
                if re.fullmatch(r"[0-9a-fA-F-]{36}", _text(value))
            )
        )[: max(1, min(int(limit or 1), 40))]
        if not normalized_ids:
            return []
        payload = self._get_json(
            "recording",
            {
                "query": " OR ".join(f"rid:{value}" for value in normalized_ids),
                "fmt": "json",
                "limit": len(normalized_ids),
            },
        )
        recordings = payload.get("recordings")
        if not isinstance(recordings, list):
            return []
        wanted = set(normalized_ids)
        return [
            recording
            for recording in recordings
            if isinstance(recording, dict)
            and _text(recording.get("id")).casefold() in wanted
        ]

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

    def browse_artist_release_groups(
        self,
        artist_id: str,
        *,
        limit: int = 12,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        normalized_artist_id = _text(artist_id)
        if not normalized_artist_id:
            return []
        payload = self._get_json(
            "release-group",
            {
                "artist": normalized_artist_id,
                "type": "album",
                "inc": "artist-credits+tags",
                "fmt": "json",
                "limit": max(1, min(int(limit or 1), 100)),
                "offset": max(int(offset or 0), 0),
            },
        )
        release_groups = payload.get("release-groups")
        if not isinstance(release_groups, list):
            return []
        return [item for item in release_groups if isinstance(item, dict)]

    def browse_release_group_releases(
        self,
        release_group_id: str,
        *,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        normalized_id = _text(release_group_id)
        if not normalized_id:
            return []
        payload = self._get_json(
            "release",
            {
                "release-group": normalized_id,
                "inc": "recordings+artist-credits+media",
                "fmt": "json",
                "limit": max(1, min(int(limit or 1), 25)),
            },
        )
        releases = payload.get("releases")
        if not isinstance(releases, list):
            return []
        return [item for item in releases if isinstance(item, dict)]


def _exact_artist_match(artist: Dict[str, Any], artist_name: str) -> bool:
    expected = _identity_text(artist_name)
    if not expected:
        return False
    names = {
        _identity_text(artist.get("name")),
        _identity_text(artist.get("sort-name")),
    }
    for alias in artist.get("aliases") or []:
        if isinstance(alias, dict):
            names.add(_identity_text(alias.get("name") or alias.get("sort-name")))
    names.discard("")
    return expected in names


def browse_musicbrainz_artist_album_items(
    artist_name: str,
    *,
    artist_id: str = "",
    client: MusicBrainzClient | None = None,
    limit: int = 12,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Browse canonical albums for one resolved MusicBrainz artist.

    Artist text is used only to resolve a canonical artist ID when the profile
    does not already carry one. Album discovery itself is an artist-ID browse,
    never a free-text album search.
    """

    try:
        resolved_client = client or MusicBrainzClient()
        resolved_artist_id = _text(artist_id)
        canonical_artist_name = _text(artist_name)
        if not resolved_artist_id:
            matches = resolved_client.search_artists(canonical_artist_name, limit=8)
            exact_matches = [
                artist
                for artist in matches
                if isinstance(artist, dict) and _exact_artist_match(artist, canonical_artist_name)
            ]
            if not exact_matches:
                return []
            exact_matches.sort(
                key=lambda artist: float(artist.get("score") or 0.0),
                reverse=True,
            )
            resolved_artist = exact_matches[0]
            resolved_artist_id = _text(resolved_artist.get("id"))
            canonical_artist_name = _text(resolved_artist.get("name")) or canonical_artist_name
        release_groups = resolved_client.browse_artist_release_groups(
            resolved_artist_id,
            limit=limit,
            offset=offset,
        )
    except Exception:
        return []

    items: List[Dict[str, Any]] = []
    for release_group in release_groups:
        if not isinstance(release_group, dict):
            continue
        primary_type = _text(release_group.get("primary-type")).casefold()
        if primary_type and primary_type != "album":
            continue
        item = musicbrainz_release_group_to_item(release_group, query=canonical_artist_name)
        if not _text(item.get("artist")):
            item["artist"] = canonical_artist_name
            item["channel"] = canonical_artist_name
        item["musicbrainz_artist_id"] = resolved_artist_id
        item["canonical_album_identity"] = (
            f"musicbrainz:release-group:{item.get('musicbrainz_release_group_id')}"
        )
        item["catalog_acquisition"] = "musicbrainz_artist_release_groups"
        if _text(item.get("title")) and _text(item.get("musicbrainz_release_group_id")):
            items.append(item)
    return items


def browse_musicbrainz_release_group_tracks(
    release_group_id: str,
    *,
    client: MusicBrainzClient | None = None,
    limit: int = 40,
) -> List[Dict[str, Any]]:
    """Return canonical recordings from the best populated release."""
    resolved_client = client or MusicBrainzClient()
    try:
        releases = resolved_client.browse_release_group_releases(
            release_group_id,
            limit=5,
        )
    except Exception:
        return []
    if not releases:
        return []

    def recording_count(release: Dict[str, Any]) -> int:
        return sum(
            len(medium.get("tracks") or [])
            for medium in release.get("media") or []
            if isinstance(medium, dict)
        )

    release = max(releases, key=recording_count)
    album = _text(release.get("title"))
    release_id = _text(release.get("id"))
    release_date = _text(release.get("date"))
    output: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for medium in release.get("media") or []:
        if not isinstance(medium, dict):
            continue
        for track in medium.get("tracks") or []:
            if not isinstance(track, dict):
                continue
            recording = track.get("recording") or {}
            if not isinstance(recording, dict):
                continue
            recording_id = _text(recording.get("id"))
            if not recording_id or recording_id in seen:
                continue
            seen.add(recording_id)
            title = _text(recording.get("title") or track.get("title"))
            artist_credit = recording.get("artist-credit") or track.get("artist-credit")
            artist = _artist_credit_name(artist_credit)
            artist_ids = _artist_credit_ids(artist_credit)
            if not title or not artist:
                continue
            output.append(
                {
                    "id": f"musicbrainz:recording:{recording_id}",
                    "title": title,
                    "artist": artist,
                    "channel": artist,
                    "album": album,
                    "duration": int(float(recording.get("length") or track.get("length") or 0) / 1000.0),
                    "musicbrainz_recording_id": recording_id,
                    "musicbrainz_artist_id": artist_ids[0] if artist_ids else "",
                    "musicbrainz_artist_ids": artist_ids,
                    "musicbrainz_release_id": release_id,
                    "musicbrainz_release_group_id": release_group_id,
                    "release_date": release_date,
                    "release_year": release_date[:4] if len(release_date) >= 4 else "",
                    "metadata_source": "musicbrainz",
                    "playable": False,
                }
            )
            if len(output) >= max(int(limit or 1), 1):
                return output
    return output


def search_musicbrainz_recording_items(
    query: str,
    *,
    artist: str = "",
    official_non_live: bool = False,
    raise_errors: bool = False,
    client: MusicBrainzClient | None = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    normalized_query = _text(query)
    normalized_artist = _text(artist)
    search_query = normalized_query
    if normalized_query and normalized_artist:
        clauses = [
            f"recording:{_lucene_phrase(normalized_query)}",
            f"artistname:{_lucene_phrase(normalized_artist)}",
        ]
        if official_non_live:
            clauses.extend(
                [
                    "status:official",
                    "-secondarytype:live",
                    "video:false",
                ]
            )
        search_query = " AND ".join(clauses)
    try:
        resolved_client = client or MusicBrainzClient()
        recordings = resolved_client.search_recordings(search_query, limit=limit)
    except Exception:
        if raise_errors:
            raise
        return []
    items = [
        musicbrainz_recording_to_item(recording, query=normalized_query)
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
