from __future__ import annotations

import os
import re
import time
from threading import RLock
from typing import Any

import requests

from ..domain.catalog import normalize_artist_name, normalize_track_title


_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SYNCED_LINE = re.compile(
    r"^\[(?P<minutes>\d{1,3}):(?P<seconds>\d{1,2}(?:\.\d{1,3})?)\](?P<text>.*)$"
)

_FOUND_TTL_SECONDS = 6 * 60 * 60
_INSTRUMENTAL_TTL_SECONDS = 24 * 60 * 60
_NOT_FOUND_TTL_SECONDS = 15 * 60
_IDENTITY_TTL_SECONDS = 5 * 60
_ERROR_TTL_SECONDS = 30


def _clean(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _response(
    *,
    track_key: str,
    lyrics_status: str,
    lines: list[dict[str, Any]] | None = None,
    has_timestamps: bool = False,
    source: str = "",
    resolved_source_id: str = "",
    provider_attempts: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    usable_lines = list(lines or [])
    return {
        # Retain the transport-level field consumed by older clients while
        # exposing the actual lyrics outcome separately.
        "status": "success",
        "lyrics_status": lyrics_status,
        "track_key": track_key,
        "video_id": resolved_source_id or track_key,
        "resolved_source_id": resolved_source_id or None,
        "has_lyrics": lyrics_status == "found" and bool(usable_lines),
        "has_timestamps": bool(has_timestamps and usable_lines),
        "source": source or None,
        "lines": usable_lines,
        "provider_attempts": list(provider_attempts or []),
    }


def _youtube_lines(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not payload:
        return []
    if payload.get("hasTimestamps"):
        output: list[dict[str, Any]] = []
        for index, line in enumerate(payload.get("lyrics") or []):
            text = _clean(getattr(line, "text", ""))
            if not text:
                continue
            output.append(
                {
                    "index": index,
                    "text": text,
                    "start_time_ms": getattr(line, "start_time", None),
                    "end_time_ms": getattr(line, "end_time", None),
                }
            )
        return output

    output = []
    for raw_line in str(payload.get("lyrics") or "").splitlines():
        text = raw_line.strip()
        if not text:
            continue
        output.append(
            {
                "index": len(output),
                "text": text,
                "start_time_ms": None,
                "end_time_ms": None,
            }
        )
    return output


def _lrclib_lines(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    synced = str(payload.get("syncedLyrics") or "").strip()
    if synced:
        timed: list[dict[str, Any]] = []
        for raw_line in synced.splitlines():
            match = _SYNCED_LINE.match(raw_line.strip())
            if not match:
                continue
            text = match.group("text").strip()
            if not text:
                continue
            start_ms = int(
                (
                    int(match.group("minutes")) * 60
                    + float(match.group("seconds"))
                )
                * 1000
            )
            timed.append(
                {
                    "index": len(timed),
                    "text": text,
                    "start_time_ms": start_ms,
                    "end_time_ms": None,
                }
            )
        for index in range(len(timed) - 1):
            timed[index]["end_time_ms"] = timed[index + 1]["start_time_ms"]
        if timed:
            return timed, True

    plain = []
    for raw_line in str(payload.get("plainLyrics") or "").splitlines():
        text = raw_line.strip()
        if not text:
            continue
        plain.append(
            {
                "index": len(plain),
                "text": text,
                "start_time_ms": None,
                "end_time_ms": None,
            }
        )
    return plain, False


def _select_lrclib_match(
    payloads: Any,
    *,
    title: str,
    artist: str,
    album: str,
    duration: int | None,
) -> dict[str, Any] | None:
    if not isinstance(payloads, list):
        return None
    expected_title = normalize_track_title(title)
    expected_artist = normalize_artist_name(artist)
    expected_album = " ".join(album.casefold().split())
    matches: list[tuple[int, dict[str, Any]]] = []
    for raw in payloads:
        if not isinstance(raw, dict):
            continue
        candidate_title = normalize_track_title(raw.get("trackName") or raw.get("name"))
        candidate_artist = normalize_artist_name(raw.get("artistName"))
        if not expected_title or candidate_title != expected_title:
            continue
        if expected_artist and candidate_artist:
            if (
                expected_artist not in candidate_artist
                and candidate_artist not in expected_artist
            ):
                continue
        candidate_duration = _positive_int(raw.get("duration"))
        if duration and candidate_duration and abs(duration - candidate_duration) > 3:
            continue
        candidate_album = " ".join(_clean(raw.get("albumName")).casefold().split())
        score = 0
        if duration and candidate_duration:
            score += 4 - min(abs(duration - candidate_duration), 3)
        if expected_album and candidate_album == expected_album:
            score += 3
        if raw.get("syncedLyrics"):
            score += 2
        if raw.get("plainLyrics") or raw.get("instrumental"):
            score += 1
        matches.append((score, raw))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


class LyricsService:
    """Resolve canonical tracks to provider lyrics without inventing identity."""

    def __init__(self, server: Any) -> None:
        self._server = server
        self._cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
        self._cache_lock = RLock()

    def get(
        self,
        track_key: str,
        *,
        title: str = "",
        artist: str = "",
        album: str = "",
        duration: int | None = None,
        source_id: str = "",
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        normalized_key = _clean(track_key)
        normalized_title = _clean(title)
        normalized_artist = _clean(artist)
        normalized_album = _clean(album)
        normalized_duration = _positive_int(duration)
        cache_key = (
            normalized_key.casefold(),
            normalized_title.casefold(),
            normalized_artist.casefold(),
            normalized_album.casefold(),
            normalized_duration,
            _clean(source_id),
        )
        cached = None if force_refresh else self._cache_get(cache_key)
        if cached is not None:
            cached["cache_hit"] = True
            return cached

        source_ids = self._youtube_source_ids(
            normalized_key,
            hinted_source_id=source_id,
        )
        attempts: list[dict[str, str]] = []
        provider_failed = False

        for youtube_id in source_ids[:3]:
            try:
                watch = self._server.ytmusic.get_watch_playlist(
                    videoId=youtube_id,
                    limit=1,
                )
                browse_id = str((watch or {}).get("lyrics") or "").strip()
                if not browse_id:
                    attempts.append({"provider": "ytmusic", "outcome": "not_found"})
                    continue
                payload = self._server.ytmusic.get_lyrics(
                    browse_id,
                    timestamps=True,
                )
                lines = _youtube_lines(payload)
                if not lines:
                    attempts.append({"provider": "ytmusic", "outcome": "not_found"})
                    continue
                attempts.append({"provider": "ytmusic", "outcome": "found"})
                result = _response(
                    track_key=normalized_key,
                    lyrics_status="found",
                    lines=lines,
                    has_timestamps=bool((payload or {}).get("hasTimestamps")),
                    source=_clean((payload or {}).get("source")) or "YouTube Music",
                    resolved_source_id=youtube_id,
                    provider_attempts=attempts,
                )
                return self._cache_put(cache_key, result)
            except Exception as exc:
                provider_failed = True
                attempts.append(
                    {
                        "provider": "ytmusic",
                        "outcome": "provider_error",
                        "error_type": type(exc).__name__,
                    }
                )

        lrclib_result = self._from_lrclib(
            track_key=normalized_key,
            title=normalized_title,
            artist=normalized_artist,
            album=normalized_album,
            duration=normalized_duration,
            resolved_source_id=source_ids[0] if source_ids else "",
        )
        if lrclib_result is not None:
            attempts.append(lrclib_result.pop("attempt"))
            lrclib_result["provider_attempts"] = attempts
            return self._cache_put(cache_key, lrclib_result)
        if normalized_title and normalized_artist:
            attempts.append({"provider": "lrclib", "outcome": "not_found"})
        elif not source_ids:
            result = _response(
                track_key=normalized_key,
                lyrics_status="identity_unresolved",
                provider_attempts=attempts,
            )
            return self._cache_put(cache_key, result)

        outcome = "provider_error" if provider_failed else "not_found"
        result = _response(
            track_key=normalized_key,
            lyrics_status=outcome,
            resolved_source_id=source_ids[0] if source_ids else "",
            provider_attempts=attempts,
        )
        return self._cache_put(cache_key, result)

    def _youtube_source_ids(
        self,
        track_key: str,
        *,
        hinted_source_id: str,
    ) -> list[str]:
        output: list[str] = []

        def add(value: Any) -> None:
            candidate = _clean(value)
            if _YOUTUBE_ID.fullmatch(candidate) and candidate not in output:
                output.append(candidate)

        add(hinted_source_id)
        add(track_key)
        if track_key:
            from ..api.stream_runtime import playback_source_candidates

            for source in playback_source_candidates(self._server, track_key):
                if str(source.get("provider") or "").strip().lower() == "youtube":
                    add(source.get("source_id"))
        return output

    def _from_lrclib(
        self,
        *,
        track_key: str,
        title: str,
        artist: str,
        album: str,
        duration: int | None,
        resolved_source_id: str,
    ) -> dict[str, Any] | None:
        if not title or not artist:
            return None
        params: dict[str, Any] = {
            "track_name": title,
            "artist_name": artist,
        }
        if album:
            params["album_name"] = album
        if duration:
            params["duration"] = duration
        base_url = os.environ.get("LRCLIB_BASE_URL", "https://lrclib.net").rstrip("/")
        user_agent = os.environ.get(
            "AURALIS_LYRICS_USER_AGENT",
            "Neatie/1.0 (https://github.com/DanBeverley/personal-music-player)",
        ).strip()
        session = getattr(self._server, "upstream_http", None) or requests
        try:
            response = session.get(
                f"{base_url}/api/get",
                params=params,
                headers={"User-Agent": user_agent},
                timeout=(3.05, 6.0),
            )
        except Exception as exc:
            return {
                "attempt": {
                    "provider": "lrclib",
                    "outcome": "provider_error",
                    "error_type": type(exc).__name__,
                },
                **_response(
                    track_key=track_key,
                    lyrics_status="provider_error",
                    resolved_source_id=resolved_source_id,
                    source="LRCLIB",
                ),
            }
        if response.status_code == 404:
            search_params: dict[str, Any] = {
                "track_name": title,
                "artist_name": artist,
            }
            if album:
                search_params["album_name"] = album
            try:
                response = session.get(
                    f"{base_url}/api/search",
                    params=search_params,
                    headers={"User-Agent": user_agent},
                    timeout=(3.05, 6.0),
                )
            except Exception as exc:
                return {
                    "attempt": {
                        "provider": "lrclib",
                        "outcome": "provider_error",
                        "error_type": type(exc).__name__,
                    },
                    **_response(
                        track_key=track_key,
                        lyrics_status="provider_error",
                        resolved_source_id=resolved_source_id,
                        source="LRCLIB",
                    ),
                }
            if response.status_code != 200:
                if response.status_code == 404:
                    return None
                return {
                    "attempt": {
                        "provider": "lrclib",
                        "outcome": "provider_error",
                        "status_code": str(response.status_code),
                    },
                    **_response(
                        track_key=track_key,
                        lyrics_status="provider_error",
                        resolved_source_id=resolved_source_id,
                        source="LRCLIB",
                    ),
                }
            try:
                payload = _select_lrclib_match(
                    response.json(),
                    title=title,
                    artist=artist,
                    album=album,
                    duration=duration,
                )
            except Exception as exc:
                return {
                    "attempt": {
                        "provider": "lrclib",
                        "outcome": "provider_error",
                        "error_type": type(exc).__name__,
                    },
                    **_response(
                        track_key=track_key,
                        lyrics_status="provider_error",
                        resolved_source_id=resolved_source_id,
                        source="LRCLIB",
                    ),
                }
            if payload is None:
                return None
        else:
            if response.status_code != 200:
                return {
                    "attempt": {
                        "provider": "lrclib",
                        "outcome": "provider_error",
                        "status_code": str(response.status_code),
                    },
                    **_response(
                        track_key=track_key,
                        lyrics_status="provider_error",
                        resolved_source_id=resolved_source_id,
                        source="LRCLIB",
                    ),
                }
            try:
                payload = response.json()
            except Exception as exc:
                return {
                    "attempt": {
                        "provider": "lrclib",
                        "outcome": "provider_error",
                        "error_type": type(exc).__name__,
                    },
                    **_response(
                        track_key=track_key,
                        lyrics_status="provider_error",
                        resolved_source_id=resolved_source_id,
                        source="LRCLIB",
                    ),
                }
        if bool(payload.get("instrumental")):
            return {
                "attempt": {"provider": "lrclib", "outcome": "instrumental"},
                **_response(
                    track_key=track_key,
                    lyrics_status="instrumental",
                    resolved_source_id=resolved_source_id,
                    source="LRCLIB",
                ),
            }
        lines, has_timestamps = _lrclib_lines(payload)
        if not lines:
            return None
        return {
            "attempt": {"provider": "lrclib", "outcome": "found"},
            **_response(
                track_key=track_key,
                lyrics_status="found",
                lines=lines,
                has_timestamps=has_timestamps,
                resolved_source_id=resolved_source_id,
                source="LRCLIB",
            ),
        }

    def _cache_get(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is None:
                return None
            expires_at, payload = cached
            if expires_at <= now:
                self._cache.pop(key, None)
                return None
            return dict(payload)

    def _cache_put(
        self,
        key: tuple[Any, ...],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        status = str(payload.get("lyrics_status") or "not_found")
        ttl = {
            "found": _FOUND_TTL_SECONDS,
            "instrumental": _INSTRUMENTAL_TTL_SECONDS,
            "not_found": _NOT_FOUND_TTL_SECONDS,
            "identity_unresolved": _IDENTITY_TTL_SECONDS,
            "provider_error": _ERROR_TTL_SECONDS,
        }.get(status, _NOT_FOUND_TTL_SECONDS)
        stored = dict(payload)
        stored["cache_hit"] = False
        with self._cache_lock:
            if len(self._cache) >= 512:
                oldest_key = min(self._cache, key=lambda item: self._cache[item][0])
                self._cache.pop(oldest_key, None)
            self._cache[key] = (time.monotonic() + ttl, stored)
        return dict(stored)
