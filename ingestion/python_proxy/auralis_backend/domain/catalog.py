from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Sequence

from ..storage.postgres import upsert_catalog_rows


_WHITESPACE_RE = re.compile(r"\s+")
_SEPARATOR_RE = re.compile(r"[\|_/]+")
_PUNCT_RE = re.compile(r"[^\w\s]")
_BRACKET_RE = re.compile(r"(\[[^\]]*\]|\([^\)]*\)|\{[^\}]*\})")
_NOISE_TOKEN_RE = re.compile(
    r"\b("
    r"acoustic|bonus|clean|deluxe|demo|edit|explicit|instrumental|karaoke|live|lyric|mix|mono|official|"
    r"radio|remaster|remastered|session|single|slowed|soundtrack|sped|stereo|topic|tribute|version|video"
    r")\b",
    re.IGNORECASE,
)
_FEAT_RE = re.compile(r"\s+(feat|featuring|ft)\.?\s+.*$", re.IGNORECASE)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _display_text(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def normalize_catalog_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value)).lower()
    text = text.replace("&", " and ")
    text = _SEPARATOR_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _strip_bracketed_noise(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        content = normalize_catalog_text(match.group(0))
        return " " if _NOISE_TOKEN_RE.search(content) else match.group(0)

    return _BRACKET_RE.sub(_replace, value)


def _strip_suffix_noise(value: str) -> str:
    parts = re.split(r"\s+-\s+", value)
    if len(parts) <= 1:
        return value
    kept: List[str] = [parts[0]]
    for part in parts[1:]:
        if _NOISE_TOKEN_RE.search(normalize_catalog_text(part)):
            continue
        kept.append(part)
    return " - ".join(kept)


def normalize_artist_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value))
    text = text.replace(" - Topic", "")
    text = _FEAT_RE.sub("", text)
    return normalize_catalog_text(text)


def normalize_track_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value))
    text = _strip_bracketed_noise(text)
    text = _strip_suffix_noise(text)
    return normalize_catalog_text(text)


def normalize_album_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value))
    text = _strip_bracketed_noise(text)
    text = _strip_suffix_noise(text)
    return normalize_catalog_text(text)


def canonical_artist_identity(artist: Dict[str, Any] | str) -> str:
    if isinstance(artist, dict):
        artist_id = _text(artist.get("id"))
        if artist_id:
            return artist_id
        return normalize_artist_name(artist.get("name") or artist.get("artist") or artist.get("channel"))
    return normalize_artist_name(artist)


def canonical_album_identity(album: Dict[str, Any]) -> str:
    album_id = _text(album.get("id") or album.get("album_id") or album.get("albumId"))
    if album_id:
        return album_id
    title_key = normalize_album_title(album.get("title") or album.get("album"))
    artist_key = normalize_artist_name(
        album.get("artist") or album.get("channel") or album.get("artist_name")
    )
    if title_key and artist_key:
        return f"{title_key}|{artist_key}"
    return title_key or artist_key


def canonical_title_artist_identity(track: Dict[str, Any]) -> str:
    title_key = normalize_track_title(track.get("title") or track.get("name"))
    artist_key = normalize_artist_name(
        track.get("channel") or track.get("artist") or track.get("author") or track.get("artist_name")
    )
    if title_key and artist_key:
        return f"{title_key}|{artist_key}"
    return title_key or artist_key


def canonical_track_identity(track: Dict[str, Any]) -> str:
    track_id = _text(track.get("id"))
    if track_id:
        return track_id
    return canonical_title_artist_identity(track)


def normalized_track_payload(track: Dict[str, Any]) -> Dict[str, Any]:
    artist_name = _display_text(track.get("channel"), track.get("artist"), track.get("author"))
    album_title = _display_text(track.get("album"))
    payload = dict(track)
    payload.update(
        {
            "artist_name": artist_name,
            "album_title": album_title,
            "normalized_title": normalize_track_title(track.get("title") or track.get("name")),
            "normalized_artist_name": normalize_artist_name(artist_name),
            "normalized_album_title": normalize_album_title(album_title),
            "canonical_track_identity": canonical_track_identity(track),
            "canonical_title_artist_identity": canonical_title_artist_identity(track),
        }
    )
    return payload


def normalized_artist_payload(artist: Dict[str, Any]) -> Dict[str, Any]:
    name = _display_text(artist.get("name"), artist.get("artist"), artist.get("channel"))
    payload = dict(artist)
    payload.update(
        {
            "name": name,
            "normalized_name": normalize_artist_name(name),
            "canonical_artist_identity": canonical_artist_identity(artist),
        }
    )
    return payload


def normalized_album_payload(album: Dict[str, Any]) -> Dict[str, Any]:
    title = _display_text(album.get("title"), album.get("album"))
    artist_name = _display_text(album.get("artist"), album.get("channel"), album.get("artist_name"))
    payload = dict(album)
    payload.update(
        {
            "title": title,
            "artist_name": artist_name,
            "normalized_title": normalize_album_title(title),
            "normalized_artist_name": normalize_artist_name(artist_name),
            "canonical_album_identity": canonical_album_identity(album),
        }
    )
    return payload


def _dedupe_rows(rows: Sequence[Dict[str, Any]], key_name: str) -> List[Dict[str, Any]]:
    deduped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = _text(row.get(key_name))
        if not key:
            continue
        deduped[key] = dict(row)
    return list(deduped.values())


def cache_tracks(tracks: Iterable[Dict]) -> None:
    rows = []
    for track in tracks or []:
        if not isinstance(track, dict):
            continue
        track_id = _text(track.get("id"))
        if not track_id:
            continue
        normalized = normalized_track_payload(track)
        rows.append(
            {
                "track_id": track_id,
                "title": _display_text(track.get("title"), track.get("name")),
                "artist_name": normalized.get("artist_name") or "",
                "album_title": normalized.get("album_title") or "",
                **normalized,
            }
        )
    upsert_catalog_rows(
        table="catalog_tracks",
        key_name="track_id",
        rows=_dedupe_rows(rows, "track_id"),
    )


def cache_artists(artists: Iterable[Dict]) -> None:
    rows = []
    for artist in artists or []:
        if not isinstance(artist, dict):
            continue
        artist_id = _text(artist.get("id"))
        if not artist_id:
            continue
        normalized = normalized_artist_payload(artist)
        rows.append({"artist_id": artist_id, **normalized})
    upsert_catalog_rows(
        table="catalog_artists",
        key_name="artist_id",
        rows=_dedupe_rows(rows, "artist_id"),
    )


def cache_albums(albums: Iterable[Dict]) -> None:
    rows = []
    for album in albums or []:
        if not isinstance(album, dict):
            continue
        album_id = _text(album.get("id"))
        if not album_id:
            continue
        normalized = normalized_album_payload(album)
        rows.append({"album_id": album_id, **normalized})
    upsert_catalog_rows(
        table="catalog_albums",
        key_name="album_id",
        rows=_dedupe_rows(rows, "album_id"),
    )


def cache_search_payload(
    *,
    tracks: List[Dict],
    artists: List[Dict],
    albums: List[Dict],
) -> None:
    cache_tracks(tracks)
    cache_artists(artists)
    cache_albums(albums)
