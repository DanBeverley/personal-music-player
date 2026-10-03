from __future__ import annotations

from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from dataclasses import replace
from difflib import SequenceMatcher
import json
import re
import time
from typing import Any, Dict, Iterable, List, Tuple

from ..domain.catalog import normalize_album_title, normalize_artist_name, normalize_track_title
from ..recommend.feature_store import load_catalog_feature, store_catalog_feature
from ..recommend.store_runtime import (
    open_recommendation_store_connection,
    open_recommendation_store_connection_without_init,
)
from ..search.intelligence import catalog_entity_key, infer_source_identity
from .config import POPULAR_RADIO_CARD_MIN_TRACKS, POPULAR_RADIO_CARD_TARGET_TRACKS
from .enrichment import MaterializedCandidateSupply
from .schema import TasteProfile
from .structured_providers import CanonicalRecording, configured_provider_value


_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PROVIDER_HEALTH_KEY = "youtube_stream_verification"
_BLOCKED_TTL_SECONDS = 30 * 60
_FORMAT_UNAVAILABLE_TTL_SECONDS = 5 * 60
_VERIFIED_TTL_SECONDS = 14 * 24 * 60 * 60
_LOOKUP_RETRY_SECONDS = 24 * 60 * 60
_EMPTY_LOOKUP_RETRY_SECONDS = 30 * 60
_LOOKUP_PROVIDER = "youtube_lookup"
_LOOKUP_SOURCE_ID = "exact_recording"
_MIN_ADAPTIVE_VERIFICATIONS = 8
_MAX_ADAPTIVE_VERIFICATIONS = 16
_MAX_VERIFICATION_WORKERS = 4
_FORMAT_FAILURE_CIRCUIT_THRESHOLD = 3
_SOURCE_MATCH_VERSION = "source-match-v2"
_NON_STUDIO_TAGS = {
    "acoustic", "cover", "demo", "instrumental", "karaoke", "live", "remix", "tribute",
}
_POOL_PRIORITY = {
    "similarity": 0,
    "artist_graph": 1,
    "profile_spine": 2,
    "genre_mood": 3,
    "collaborative": 4,
    "popularity": 5,
}


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _json_loads(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _recording_key(item: Dict[str, Any]) -> str:
    canonical = _text(item.get("canonical_entity_id"))
    if canonical:
        return canonical.removeprefix("track:")
    return catalog_entity_key("track", item)


def _track_key(item: Dict[str, Any]) -> str:
    mbid = _text(item.get("musicbrainz_recording_id") or item.get("recording_mbid"))
    if mbid:
        return f"recording:{mbid}"
    key = _recording_key(item)
    if key.startswith("musicbrainz:recording:"):
        return f"recording:{key.removeprefix('musicbrainz:recording:')}"
    if key.startswith("recording:"):
        return key
    return f"recording:{key}" if key else ""


def _artist(item: Dict[str, Any]) -> str:
    value = item.get("artist") or item.get("artist_name") or item.get("channel")
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


def _duration(value: Any) -> int:
    if isinstance(value, (int, float)):
        number = int(value)
        return number // 1000 if number > 10000 else max(number, 0)
    text = _text(value)
    if text.startswith("PT"):
        match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text)
        if match:
            return int(match.group(1) or 0) * 3600 + int(match.group(2) or 0) * 60 + int(match.group(3) or 0)
    if ":" in text:
        try:
            total = 0
            for part in text.split(":"):
                total = total * 60 + int(part)
            return total
        except ValueError:
            return 0
    return int(text) if text.isdigit() else 0


def _source_rank(source: Dict[str, Any]) -> Tuple[int, float, float, float]:
    authority = _text(source.get("authority")).casefold()
    authority_rank = {
        "official": 5,
        "official_artist_channel": 5,
        "topic": 4,
        "label": 4,
        "vevo": 4,
        "verified_catalog": 3,
        "album_relation": 3,
        "trusted_match": 2,
    }.get(authority, 1)
    return (
        authority_rank,
        float(source.get("identity_confidence") or source.get("confidence") or 0.0),
        float(source.get("duration_match_score") or 0.0),
        float(source.get("verified_at") or 0.0),
    )


def _version_tags(*values: Any) -> set[str]:
    text = " ".join(_text(value).casefold() for value in values if _text(value))
    patterns = {
        "live": r"\b(live|concert|festival|unplugged)\b",
        "acoustic": r"\bacoustic\b",
        "demo": r"\b(demo|rehearsal|session)\b",
        "remix": r"\b(remix|mix edit|club mix)\b",
        "instrumental": r"\binstrumental\b",
        "karaoke": r"\bkaraoke\b",
        "cover": r"\bcover(?: version)?\b",
        "tribute": r"\btribute\b",
    }
    return {tag for tag, pattern in patterns.items() if re.search(pattern, text)}


def _version_is_compatible(
    recording: CanonicalRecording,
    source: Dict[str, Any],
) -> tuple[bool, set[str], set[str]]:
    expected = _version_tags(recording.title, recording.album)
    actual = _version_tags(source.get("title"), source.get("album"), source.get("channel"))
    expected_special = expected & _NON_STUDIO_TAGS
    actual_special = actual & _NON_STUDIO_TAGS
    if expected_special:
        return (
            bool(expected_special & actual_special)
            and not bool(actual_special - expected_special),
            expected,
            actual,
        )
    return not actual_special, expected, actual


def playback_source_semantically_compatible(source: Dict[str, Any]) -> bool:
    """Reject known wrong editions before a persisted source reaches playback."""

    if _text(source.get("source_match_version")) == _SOURCE_MATCH_VERSION:
        return source.get("match_compatible") is True
    # Old rows only proved that a physical stream once resolved. They did not
    # prove that it represented this canonical recording, so force them back
    # through the current title/artist/version matcher on demand.
    return False


def _rematch_pending_source(
    recording: CanonicalRecording,
    source: Dict[str, Any],
) -> Dict[str, Any] | None:
    if not _text(source.get("title")) or not _text(source.get("channel")):
        return source if playback_source_semantically_compatible(source) else None
    return _match_source(recording, source)


def _load_sources(server: Any, entity_keys: Iterable[str]) -> Dict[str, List[Dict[str, Any]]]:
    keys = list(dict.fromkeys(_text(value) for value in entity_keys if _text(value)))
    if not keys:
        return {}
    connection = open_recommendation_store_connection(server)
    output: Dict[str, List[Dict[str, Any]]] = {}
    try:
        for start in range(0, len(keys), 100):
            batch = keys[start : start + 100]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""
                SELECT entity_key, source_provider, source_key, source_authority,
                       confidence, payload_json, updated_at
                FROM catalog_entity_sources
                WHERE entity_type = 'track' AND entity_key IN ({placeholders})
                """,
                batch,
            ).fetchall()
            for row in rows:
                payload = _json_loads(row["payload_json"])
                output.setdefault(str(row["entity_key"]), []).append(
                    {
                        **payload,
                        "provider": str(row["source_provider"] or "").lower(),
                        "source_id": str(row["source_key"] or ""),
                        "authority": str(row["source_authority"] or ""),
                        "confidence": float(row["confidence"] or 0.0),
                        "updated_at": float(row["updated_at"] or 0.0),
                    }
                )
    finally:
        connection.close()
    return output


def _store_sources(
    server: Any,
    sources: Iterable[Tuple[str, Dict[str, Any]]],
    *,
    store_initialized: bool = False,
) -> int:
    rows: Dict[Tuple[str, str, str], List[Any]] = {}
    now = time.time()
    for raw_entity_key, raw_source in sources:
        entity_key = _text(raw_entity_key)
        source = dict(raw_source or {})
        provider = _text(source.get("provider")).casefold()
        source_id = _text(source.get("source_id"))
        if not entity_key or not provider or not source_id:
            continue
        payload = {
            **source,
            "provider": provider,
            "source_id": source_id,
            "updated_at": now,
        }
        rows[(entity_key, provider, source_id)] = [
            entity_key,
            provider,
            source_id,
            _text(source.get("authority")) or "trusted_match",
            float(source.get("identity_confidence") or source.get("confidence") or 0.0),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            now,
        ]
    if not rows:
        return 0
    connection = (
        open_recommendation_store_connection_without_init(server)
        if store_initialized
        else open_recommendation_store_connection(server)
    )
    try:
        entity_keys = list(dict.fromkeys(key[0] for key in rows))
        existing: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for start in range(0, len(entity_keys), 100):
            batch = entity_keys[start : start + 100]
            placeholders = ",".join("?" for _ in batch)
            for current in connection.execute(
                f"""
                SELECT entity_key, source_provider, source_key, payload_json
                FROM catalog_entity_sources
                WHERE entity_type = 'track' AND entity_key IN ({placeholders})
                """,
                batch,
            ).fetchall():
                existing[
                    (
                        str(current["entity_key"]),
                        str(current["source_provider"]),
                        str(current["source_key"]),
                    )
                ] = _json_loads(current["payload_json"])
        now = time.time()
        for key, values in list(rows.items()):
            current = existing.get(key) or {}
            current_state = _text(current.get("verification_state"))
            incoming = json.loads(values[5])
            incoming_state = _text(incoming.get("verification_state"))
            if (
                current_state == "verified"
                and incoming_state not in {"verified", "identity_mismatch"}
            ):
                # Rediscovery may improve metadata, but it must not erase the
                # proof that this exact provider source already resolved.
                for field in (
                    "verification_state",
                    "verified_at",
                    "verification_method",
                ):
                    if current.get(field) is not None:
                        incoming[field] = current[field]
                values[5] = json.dumps(
                    incoming,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                incoming_state = "verified"
            terminal_unavailable = current_state == "unavailable"
            blocked_until_retry = (
                current_state == "temporarily_blocked"
                and float(current.get("retry_at") or 0.0) > now
            )
            if (
                incoming_state in {"", "pending"}
                and (terminal_unavailable or blocked_until_retry)
            ):
                rows.pop(key, None)
        if not rows:
            return 0
        connection.executemany(
            """
            INSERT INTO catalog_entity_sources(
                entity_type, entity_key, source_provider, source_key,
                source_authority, confidence, payload_json, updated_at
            ) VALUES ('track', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_type, entity_key, source_provider, source_key) DO UPDATE SET
                source_authority = excluded.source_authority,
                confidence = max(catalog_entity_sources.confidence, excluded.confidence),
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            list(rows.values()),
        )
        connection.commit()
    finally:
        connection.close()
    return len(rows)


def _verified_source(
    sources: Iterable[Dict[str, Any]],
    item: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    now = time.time()
    verified = [
        source
        for source in sources
        if source.get("verification_state") == "verified"
        and now - float(source.get("verified_at") or 0.0) <= _VERIFIED_TTL_SECONDS
    ]
    if item and _text(item.get("title")) and _artist(item):
        recording = CanonicalRecording.from_item(item)
        compatible: List[Dict[str, Any]] = []
        for source in verified:
            if not _text(source.get("title")) or not _text(source.get("channel")):
                if playback_source_semantically_compatible(source):
                    compatible.append(source)
                continue
            match = _match_source(recording, source)
            if match is not None:
                compatible.append({**source, **match, "verification_state": "verified"})
        verified = compatible
    return max(verified, key=_source_rank) if verified else None


def _lookup_is_deferred(sources: Iterable[Dict[str, Any]], *, now: float) -> bool:
    return any(
        source.get("provider") == _LOOKUP_PROVIDER
        and source.get("source_id") == _LOOKUP_SOURCE_ID
        and float(source.get("retry_at") or 0.0) > now
        for source in sources
    )


def _lookup_no_match_is_deferred(
    sources: Iterable[Dict[str, Any]],
    *,
    now: float,
) -> bool:
    """Whether a prior empty lookup is still cooling down.

    Enumerated lookups may have pending source candidates that still need
    verification, so only an explicit no-match marker is excluded from a new
    bounded shortlist.
    """

    rows = list(sources)
    # A source learned after an empty lookup must still reach verification.
    # Semantic rematching remains the responsibility of the verifier below.
    if any(source.get("provider") == "youtube"
           and source.get("verification_state") == "pending"
           for source in rows):
        return False
    return any(
        source.get("provider") == _LOOKUP_PROVIDER
        and source.get("source_id") == _LOOKUP_SOURCE_ID
        and source.get("verification_state") == "no_match"
        and float(source.get("retry_at") or 0.0) > now
        for source in rows
    )


def _lookup_marker(candidates: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    source_ids = list(
        dict.fromkeys(
            _text(source.get("source_id"))
            for source in candidates
            if _text(source.get("source_id"))
        )
    )
    now = time.time()
    return {
        "provider": _LOOKUP_PROVIDER,
        "source_id": _LOOKUP_SOURCE_ID,
        "authority": "canonical_resolution",
        "verification_state": "enumerated" if source_ids else "no_match",
        "returned_source_ids": source_ids,
        "attempted_at": now,
        "retry_at": now
        + (_LOOKUP_RETRY_SECONDS if source_ids else _EMPTY_LOOKUP_RETRY_SECONDS),
    }


def _provider_is_blocked(server: Any) -> bool:
    payload = load_catalog_feature(
        server,
        entity_type="playback_provider_health",
        entity_key=_PROVIDER_HEALTH_KEY,
    ) or {}
    return (
        payload.get("state")
        in {"temporarily_blocked", "temporarily_unavailable"}
        and float(payload.get("retry_at") or 0.0) > time.time()
    )


def youtube_background_resolution_blocked(server: Any) -> bool:
    """Shared circuit for automatic verification and stream warming only."""

    return _provider_is_blocked(server)


def _set_provider_health(
    server: Any,
    *,
    blocked: bool,
    failures: int = 0,
    reason: str = "source_blocked",
) -> None:
    now = time.time()
    existing = load_catalog_feature(
        server,
        entity_type="playback_provider_health",
        entity_key=_PROVIDER_HEALTH_KEY,
    ) or {}
    store_catalog_feature(
        server,
        entity_type="playback_provider_health",
        entity_key=_PROVIDER_HEALTH_KEY,
        payload={
            **existing,
            "state": (
                "temporarily_unavailable"
                if blocked and reason == "format_unavailable"
                else "temporarily_blocked"
                if blocked
                else "available"
            ),
            "consecutive_blocked_failures": int(failures if blocked else 0),
            "failure_reason": reason if blocked else "",
            "retry_at": (
                now
                + (
                    _FORMAT_UNAVAILABLE_TTL_SECONDS
                    if reason == "format_unavailable"
                    else _BLOCKED_TTL_SECONDS
                )
                if blocked
                else 0.0
            ),
            "updated_at": now,
        },
        source_kind="playback_verification",
        confidence=1.0,
    )


def record_youtube_background_failures(
    server: Any,
    failures: Dict[str, Dict[str, Any]],
) -> None:
    blocked = sum(
        1
        for failure in (failures or {}).values()
        if isinstance(failure, dict) and failure.get("code") == "source_blocked"
    )
    if blocked:
        _set_provider_health(server, blocked=True, failures=blocked)


def _youtube_api_get(server: Any, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
    key = configured_provider_value("YOUTUBE_DATA_API_KEY")
    if not key:
        return {}
    response = server.upstream_http.get(
        f"https://www.googleapis.com/youtube/v3/{endpoint}",
        params={**params, "key": key},
        timeout=8,
    )
    response.raise_for_status()
    payload = response.json()
    return dict(payload) if isinstance(payload, dict) else {}


def _youtube_metadata(server: Any, video_ids: Iterable[str]) -> List[Dict[str, Any]]:
    ids = [value for value in dict.fromkeys(video_ids) if _YOUTUBE_ID_RE.fullmatch(value)]
    if not ids:
        return []
    payload = _youtube_api_get(
        server,
        "videos",
        {"part": "snippet,contentDetails,status", "id": ",".join(ids[:50])},
    )
    output: List[Dict[str, Any]] = []
    for row in payload.get("items") or []:
        snippet = row.get("snippet") or {}
        status = row.get("status") or {}
        if status.get("privacyStatus") not in {None, "public"} or status.get("uploadStatus") not in {None, "processed"}:
            continue
        output.append(
            {
                "source_id": _text(row.get("id")),
                "title": _text(snippet.get("title")),
                "channel": _text(snippet.get("channelTitle")),
                "channel_id": _text(snippet.get("channelId")),
                "duration": _duration((row.get("contentDetails") or {}).get("duration")),
                "thumbnail": (((snippet.get("thumbnails") or {}).get("high") or {}).get("url") or ""),
            }
        )
    return output


def _youtube_channel_thumbnails(
    server: Any,
    channel_ids: Iterable[str],
) -> Dict[str, str]:
    ids = [
        value
        for value in dict.fromkeys(_text(value) for value in channel_ids)
        if value
    ][:50]
    if not ids:
        return {}
    payload = _youtube_api_get(
        server,
        "channels",
        {"part": "snippet", "id": ",".join(ids)},
    )
    output: Dict[str, str] = {}
    for row in payload.get("items") or []:
        if not isinstance(row, dict):
            continue
        snippet = row.get("snippet") or {}
        thumbnails = snippet.get("thumbnails") or {}
        thumbnail = (
            (thumbnails.get("high") or {}).get("url")
            or (thumbnails.get("medium") or {}).get("url")
            or (thumbnails.get("default") or {}).get("url")
            or ""
        )
        channel_id = _text(row.get("id"))
        if channel_id and thumbnail:
            output[channel_id] = _text(thumbnail)
    return output


def _artist_browse_ids(source: Dict[str, Any]) -> List[str]:
    raw = source.get("artist_browse_ids") or []
    if isinstance(raw, str):
        raw = [raw]
    return list(
        dict.fromkeys(
            _text(value)
            for value in raw
            if _text(value)
        )
    )


def _match_source(recording: CanonicalRecording, source: Dict[str, Any]) -> Dict[str, Any] | None:
    version_compatible, expected_tags, actual_tags = _version_is_compatible(recording, source)
    if not version_compatible:
        return None
    wanted_title = normalize_track_title(recording.title)
    actual_title = normalize_track_title(source.get("title"))
    wanted_artist = normalize_artist_name(recording.artist)
    actual_artist = normalize_artist_name(source.get("channel"))
    catalog_artist = normalize_artist_name(source.get("catalog_artist"))
    title_score = SequenceMatcher(None, wanted_title, actual_title).ratio() if wanted_title and actual_title else 0.0
    artist_score = SequenceMatcher(None, wanted_artist, actual_artist).ratio() if wanted_artist and actual_artist else 0.0
    if wanted_artist and actual_artist and (wanted_artist in actual_artist or actual_artist in wanted_artist):
        artist_score = max(artist_score, 0.92)
    if wanted_artist and catalog_artist:
        catalog_artist_score = SequenceMatcher(None, wanted_artist, catalog_artist).ratio()
        if wanted_artist in catalog_artist or catalog_artist in wanted_artist:
            catalog_artist_score = max(catalog_artist_score, 0.96)
        artist_score = max(artist_score, catalog_artist_score)
    duration_score = 0.5
    actual_duration = _duration(source.get("duration"))
    if recording.duration_seconds and actual_duration:
        delta = abs(recording.duration_seconds - actual_duration)
        if delta > max(20, int(recording.duration_seconds * 0.12)):
            return None
        duration_score = 1.0 if delta <= 5 else 0.8 if delta <= 12 else 0.65
    wanted_album = normalize_album_title(recording.album)
    actual_album = normalize_album_title(source.get("album"))
    album_score = (
        SequenceMatcher(None, wanted_album, actual_album).ratio()
        if wanted_album and actual_album
        else 0.5
    )
    identity = infer_source_identity(
        {
            "id": source.get("source_id"),
            "videoId": source.get("source_id"),
            "title": source.get("title"),
            "channel": source.get("channel"),
            "provider": "youtube",
            "channel_id": source.get("channel_id") or source.get("channel"),
        }
    )
    authority = _text(identity.get("authority")) or "user_upload"
    if authority == "user_upload" and source.get("ytmusic_catalog") is True:
        authority = "verified_catalog"
    confidence = title_score * 0.5 + artist_score * 0.3 + duration_score * 0.15 + album_score * 0.05
    if title_score < 0.78 or artist_score < 0.78 or confidence < 0.76:
        return None
    return {
        **source,
        "provider": "youtube",
        "authority": authority,
        "identity_confidence": round(confidence, 4),
        "title_match_score": round(title_score, 4),
        "artist_match_score": round(artist_score, 4),
        "duration_match_score": round(duration_score, 4),
        "album_match_score": round(album_score, 4),
        "source_match_version": _SOURCE_MATCH_VERSION,
        "match_compatible": True,
        "canonical_title": recording.title,
        "canonical_artist": recording.artist,
        "canonical_album": recording.album,
        "canonical_duration": recording.duration_seconds,
        "canonical_version_tags": sorted(expected_tags),
        "source_version_tags": sorted(actual_tags),
        "verification_state": "pending",
    }


def _ytmusic_song_source(server: Any, source_id: str) -> Dict[str, Any]:
    """Read provider-owned identity fields for one physical source hint."""

    try:
        payload = server.ytmusic.get_song(source_id) or {}
    except Exception:
        return {}
    details = payload.get("videoDetails") or {}
    if not isinstance(details, dict):
        return {}
    thumbnails = (details.get("thumbnail") or {}).get("thumbnails") or []
    return {
        "source_id": source_id,
        "title": _text(details.get("title")),
        "channel": _text(details.get("author")),
        "channel_id": _text(details.get("channelId")),
        "catalog_artist": _text(details.get("author")),
        "duration": _duration(details.get("lengthSeconds")),
        "thumbnail": _text(
            next(
                (
                    entry.get("url")
                    for entry in reversed(thumbnails)
                    if isinstance(entry, dict) and _text(entry.get("url"))
                ),
                "",
            )
        ),
        "ytmusic_catalog": True,
    }


def _enumerate_youtube_sources(server: Any, item: Dict[str, Any]) -> List[Dict[str, Any]]:
    recording = CanonicalRecording.from_item(item)
    if not recording.title or not recording.artist:
        return []
    query = f"{recording.title} {recording.artist}"
    direct_id = _text(
        item.get("playable_source_id")
        or item.get("videoId")
        or item.get("video_id")
        or item.get("id")
    )
    direct_provider = _text(
        item.get("source_provider") or item.get("provider")
    ).casefold()
    video_ids: List[str] = []
    ytmusic_evidence: Dict[str, Dict[str, Any]] = {}
    if (
        direct_provider in {"ytmusic", "youtube", "youtube_music"}
        and _YOUTUBE_ID_RE.fullmatch(direct_id)
    ):
        video_ids.append(direct_id)
        ytmusic_evidence[direct_id] = {
            **_ytmusic_song_source(server, direct_id),
            "catalog_artist": recording.artist,
            "album": _text(item.get("album") or item.get("album_title")),
            "album_id": _text(item.get("album_id")),
            "ytmusic_catalog": True,
        }
    try:
        raw = server.ytmusic.search(query, filter="songs", limit=8) or []
    except Exception:
        raw = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        video_id = _text(row.get("videoId") or row.get("id"))
        if not video_id:
            continue
        video_ids.append(video_id)
        artists = [entry for entry in row.get("artists") or [] if isinstance(entry, dict)]
        album = row.get("album") if isinstance(row.get("album"), dict) else {}
        ytmusic_evidence[video_id] = {
            "source_id": video_id,
            "title": _text(row.get("title")),
            "channel": _text(
                ", ".join(
                    _text(entry.get("name"))
                    for entry in artists
                    if _text(entry.get("name"))
                )
            )
            or _artist(row),
            "catalog_artist": _text(
                ", ".join(_text(entry.get("name")) for entry in artists if _text(entry.get("name")))
            ),
            "artist_browse_ids": [
                _text(entry.get("id") or entry.get("browseId"))
                for entry in artists
                if _text(entry.get("id") or entry.get("browseId"))
            ],
            "album": _text(album.get("name") or album.get("title")),
            "album_id": _text(album.get("id") or album.get("browseId")),
            "duration": _duration(
                row.get("duration_seconds")
                or row.get("duration")
                or row.get("length")
            ),
            "thumbnail": _text(
                next(
                    (
                        entry.get("url")
                        for entry in reversed(row.get("thumbnails") or [])
                        if isinstance(entry, dict) and _text(entry.get("url"))
                    ),
                    "",
                )
            ),
            "ytmusic_video_type": _text(row.get("videoType")),
            "ytmusic_catalog": bool(
                album
                or any(_text(entry.get("id") or entry.get("browseId")) for entry in artists)
                or _text(row.get("videoType")).startswith("MUSIC_VIDEO_TYPE_")
            ),
        }
    try:
        youtube_metadata = _youtube_metadata(server, video_ids)
    except Exception:
        youtube_metadata = []
    metadata_by_id = {
        _text(row.get("source_id")): dict(row)
        for row in youtube_metadata
        if _text(row.get("source_id"))
    }
    # YTMusic catalog rows remain useful identity evidence when the optional
    # YouTube Data API is not configured or temporarily unavailable. Stream
    # resolution below is still mandatory before publication.
    metadata = [
        {
            **ytmusic_evidence.get(source_id, {}),
            **metadata_by_id.get(source_id, {}),
            **{
                key: value
                for key, value in ytmusic_evidence.get(source_id, {}).items()
                if key.startswith("ytmusic_")
                or key in {"catalog_artist", "artist_browse_ids", "album", "album_id"}
            },
        }
        for source_id in dict.fromkeys(video_ids)
        if source_id in metadata_by_id or source_id in ytmusic_evidence
    ]
    matched = [source for source in (_match_source(recording, row) for row in metadata) if source]
    if not matched and configured_provider_value("YOUTUBE_DATA_API_KEY"):
        try:
            search_payload = _youtube_api_get(
                server,
                "search",
                {
                    "part": "snippet",
                    "q": query,
                    "type": "video",
                    "videoCategoryId": "10",
                    "maxResults": 8,
                },
            )
            searched_ids = [
                _text((row.get("id") or {}).get("videoId"))
                for row in search_payload.get("items") or []
                if isinstance(row, dict)
            ]
            metadata = _youtube_metadata(server, searched_ids)
            matched = [source for source in (_match_source(recording, row) for row in metadata) if source]
        except Exception:
            matched = []
    matched.sort(key=_source_rank, reverse=True)
    return matched


def resolve_user_requested_playback_source(
    server: Any,
    *,
    track_key: str,
    title: str = "",
    artist: str = "",
    album: str = "",
    duration: int = 0,
    source_id: str = "",
    source_provider: str = "youtube",
) -> Dict[str, Any] | None:
    """Resolve one canonical recording for an explicit playback request.

    This is the synchronous, user-priority counterpart to bounded background
    supply verification. It never accepts a physical hint by ID alone: the
    provider metadata must match the requested recording and the stream must
    resolve before the source is persisted as verified.
    """

    normalized_key = _text(track_key)
    normalized_source_id = _text(source_id)
    normalized_provider = _text(source_provider).casefold()
    if normalized_provider in {"ytmusic", "youtube_music"}:
        normalized_provider = "youtube"
    is_canonical = normalized_key.startswith(
        ("recording:", "musicbrainz:recording:")
    )
    if not is_canonical:
        return None

    recording_id = normalized_key.removeprefix("recording:").removeprefix(
        "musicbrainz:recording:"
    )
    canonical_recording_id = (
        recording_id
        if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", recording_id)
        else ""
    )
    item: Dict[str, Any] = {
        "track_key": normalized_key,
        "title": _text(title),
        "artist": _text(artist),
        "channel": _text(artist),
        "album": _text(album),
        "duration": _duration(duration),
        "musicbrainz_recording_id": recording_id if not canonical_recording_id else "",
        "canonical_entity_id": canonical_recording_id,
    }
    if normalized_provider == "youtube" and _YOUTUBE_ID_RE.fullmatch(
        normalized_source_id
    ):
        item.update(
            {
                "id": normalized_source_id,
                "videoId": normalized_source_id,
                "playable_source_id": normalized_source_id,
                "provider": "youtube",
                "source_provider": "youtube",
            }
        )

    entity_key = _recording_key(item)
    if not entity_key or not item["title"] or not item["artist"]:
        return None
    known_sources = _load_sources(server, [entity_key]).get(entity_key) or []
    existing = _verified_source(known_sources, item)
    if existing is not None:
        print(
            "[EBB:playback-source] "
            f"track={normalized_key} outcome=registry_verified "
            f"source={_text(existing.get('source_id')) or '-'}"
        )
        return existing

    candidates = _enumerate_youtube_sources(server, item)
    if not candidates:
        print(
            "[EBB:playback-source] "
            f"track={normalized_key} outcome=no_compatible_source "
            f"hint={normalized_source_id or '-'}"
        )
        return None
    _store_sources(server, [(entity_key, source) for source in candidates])
    rejected_ids = {
        _text(source.get("source_id"))
        for source in known_sources
        if source.get("verification_state") == "unavailable"
        or (
            source.get("verification_state") == "temporarily_blocked"
            and float(source.get("retry_at") or 0.0) > time.time()
        )
    }
    for candidate in candidates[:4]:
        if _text(candidate.get("source_id")) in rejected_ids:
            continue
        verified, outcome = _verify_source(server, entity_key, candidate)
        if verified is not None:
            _store_sources(server, [(entity_key, verified)])
        if verified is not None and outcome in {
            "verified_cache",
            "verified_resolver",
        }:
            print(
                "[EBB:playback-source] "
                f"track={normalized_key} outcome={outcome} "
                f"source={_text(verified.get('source_id')) or '-'}"
            )
            return verified
    print(
        "[EBB:playback-source] "
        f"track={normalized_key} outcome=verification_failed "
        f"candidates={len(candidates[:4])}"
    )
    return None


def _verify_source(server: Any, entity_key: str, source: Dict[str, Any]) -> Tuple[Dict[str, Any] | None, str]:
    from ..api.stream_cache import get_stream_cache
    from ..api.stream_core_runtime import classify_stream_failure, get_stream_info

    source_id = _text(source.get("source_id"))
    now = time.time()
    cache = get_stream_cache(server)
    try:
        cached = cache.head(source_id) if cache.enabled else None
        if cached:
            verified = {
                **source,
                "verification_state": "verified",
                "verified_at": now,
                "verification_method": "stream_cache",
            }
            return verified, "verified_cache"
        info = get_stream_info(server, source_id)
        if not _text((info or {}).get("url")):
            raise RuntimeError("stream_url_missing")
        verified = {
            **source,
            "verification_state": "verified",
            "verified_at": now,
            "verification_method": "stream_resolver",
        }
        return verified, "verified_resolver"
    except Exception as exc:
        failure = classify_stream_failure(exc)
        blocked = failure.get("code") == "source_blocked"
        failed = {
            **source,
            "verification_state": "temporarily_blocked" if blocked else "unavailable",
            "last_failure_at": now,
            "failure_reason": failure.get("code") or "stream_failed",
            "retry_at": now + _BLOCKED_TTL_SECONDS if blocked else 0.0,
        }
        return failed, (
            "source_blocked"
            if blocked
            else str(failure.get("code") or "unavailable")
        )


def _apply_source(item: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    source_id = _text(source.get("source_id"))
    track_key = _track_key(item)
    authority = _text(source.get("authority")) or "trusted_match"
    artist_ids = _artist_browse_ids(source)
    artist_id = _text(
        item.get("artist_id")
        or item.get("artistId")
        or item.get("artist_browse_id")
        or (artist_ids[0] if artist_ids else "")
        or (
            source.get("channel_id")
            if authority in {"official_artist_channel", "topic", "vevo"}
            else ""
        )
    )
    return {
        **dict(item),
        "id": source_id,
        "videoId": source_id,
        "track_key": track_key,
        "canonical_entity_id": _recording_key(item),
        "playable_source_id": source_id,
        "provider": "youtube",
        "source_provider": "youtube",
        "source_authority": "official" if authority in {"official_artist_channel", "vevo"} else authority,
        "source_identity_authority": authority,
        "source_resolution_confidence": float(source.get("identity_confidence") or source.get("confidence") or 0.0),
        "thumbnail": _text(item.get("thumbnail") or source.get("thumbnail"))
        or f"https://i.ytimg.com/vi/{source_id}/hqdefault.jpg",
        "channel": _text(item.get("channel") or source.get("channel")),
        "artist_id": artist_id,
        "artist_thumbnail": _text(
            item.get("artist_thumbnail") or source.get("artist_thumbnail")
        ),
        "playable": True,
        "playback": {
            "provider": "youtube",
            "source_id": source_id,
            "authority": authority,
            "verified_at": float(source.get("verified_at") or time.time()),
        },
    }


def _shortlist(
    supply: MaterializedCandidateSupply,
    taste: TasteProfile,
    verified_by_key: Dict[str, Dict[str, Any]],
    *,
    limit: int,
    rejection_counts: Dict[str, int] | None = None,
    source_registry_by_key: Dict[str, List[Dict[str, Any]]] | None = None,
    now: float | None = None,
) -> List[Tuple[str, str, Dict[str, Any]]]:
    history_keys = {_recording_key(item) for item in taste.full_history_tracks if isinstance(item, dict)}
    rejected = rejection_counts if rejection_counts is not None else {}
    registry = source_registry_by_key or {}
    current_time = time.time() if now is None else float(now)
    deferred_keys: set[str] = set()

    def lookup_cooling_down(key: str) -> bool:
        deferred = _lookup_no_match_is_deferred(
            registry.get(key) or [],
            now=current_time,
        )
        if deferred:
            deferred_keys.add(key)
            rejected["lookup_cooldown"] = len(deferred_keys)
        return deferred

    queues: Dict[str, List[Tuple[Tuple[int, float, float, str], str, Dict[str, Any]]]] = {}

    def verification_rank(item: Dict[str, Any], key: str) -> Tuple[int, float, float, str] | None:
        if not _text(item.get("title")) or not _artist(item):
            rejected["missing_identity"] = rejected.get("missing_identity", 0) + 1
            return None
        authority = _text(item.get("source_authority")).casefold()
        if authority == "search_only":
            rejected["search_only"] = rejected.get("search_only", 0) + 1
            return None
        feedback = _text(item.get("negative_feedback_state")).casefold()
        if feedback in {"hidden", "removed", "hard_suppressed"}:
            rejected["negative_feedback"] = rejected.get("negative_feedback", 0) + 1
            return None

        canonical_quality = 0.0
        if _text(item.get("musicbrainz_recording_id") or item.get("recording_mbid")):
            canonical_quality += 4.0
        elif _text(item.get("isrc")):
            canonical_quality += 3.0
        if _duration(item.get("duration") or item.get("duration_seconds") or item.get("duration_ms")):
            canonical_quality += 1.0
        if _text(item.get("year") or item.get("release_year") or item.get("date")):
            canonical_quality += 0.5
        if _text(item.get("language")) not in {"", "unknown"}:
            canonical_quality += 0.5
        relation_score = float(item.get("relationship_score") or item.get("listen_count") or 0.0)
        return (1 if key in history_keys else 0, -canonical_quality, -relation_score, key)

    album_groups: List[Tuple[int, int, List[Tuple[Tuple[int, float, float, str], str, Dict[str, Any]]]]] = []
    radio_groups: Dict[str, List[Dict[str, Any]]] = {}
    radio_group_order: Dict[str, int] = {}
    for pool, items in supply.pools.items():
        if pool == "album":
            for album_index, album in enumerate(items or []):
                canonical_tracks = [
                    item
                    for item in album.get("canonical_tracks") or []
                    if isinstance(item, dict)
                ]
                required = (
                    len(canonical_tracks)
                    if len(canonical_tracks) < 6
                    else max(6, int(len(canonical_tracks) * 0.8 + 0.999))
                )
                verified_count = sum(
                    _recording_key(item) in verified_by_key for item in canonical_tracks
                )
                missing = max(required - verified_count, 0)
                if not missing:
                    continue
                unresolved: List[
                    Tuple[Tuple[int, float, float, str], str, Dict[str, Any]]
                ] = []
                for item in canonical_tracks:
                    if not isinstance(item, dict):
                        continue
                    key = _recording_key(item)
                    if not key or key in verified_by_key or lookup_cooling_down(key):
                        continue
                    rank = verification_rank(item, key)
                    if rank is not None:
                        unresolved.append((rank, key, item))
                unresolved.sort(key=lambda row: row[0])
                if unresolved:
                    album_groups.append((missing, album_index, unresolved))
            continue
        if pool == "radio_artist_catalog":
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                seed_names = list(item.get("radio_seed_artists") or [])
                if item.get("radio_seed_artist"):
                    seed_names.append(item.get("radio_seed_artist"))
                if not seed_names and item.get("related_to_artist"):
                    seed_names.append(item.get("related_to_artist"))
                for seed_name in dict.fromkeys(_text(value) for value in seed_names):
                    seed_key = seed_name.casefold()
                    if not seed_key:
                        continue
                    radio_group_order.setdefault(seed_key, len(radio_group_order))
                    radio_groups.setdefault(seed_key, []).append(item)
            continue
        queue = queues.setdefault(pool, [])
        for item in items or []:
            if not isinstance(item, dict):
                continue
            key = _recording_key(item)
            if not key or key in verified_by_key or lookup_cooling_down(key):
                continue
            rank = verification_rank(item, key)
            if rank is not None:
                queue.append((rank, key, item))
    for queue in queues.values():
        queue.sort(key=lambda row: row[0])

    if album_groups:
        album_groups.sort(key=lambda value: (value[0], value[1]))
        queues["album"] = [
            row
            for _missing, _album_index, unresolved in album_groups
            for row in unresolved
        ]

    radio_queue: List[Tuple[Tuple[int, float, float, str], str, Dict[str, Any]]] = []
    ordered_radio_groups = []
    for seed_key, items in radio_groups.items():
        verified_items = [
            item for item in items if _recording_key(item) in verified_by_key
        ]
        unresolved = [
            item
            for item in items
            if _recording_key(item) not in verified_by_key
            and not lookup_cooling_down(_recording_key(item))
        ]
        if not unresolved:
            continue
        verified_count = len({_recording_key(item) for item in verified_items})
        gap = max(POPULAR_RADIO_CARD_MIN_TRACKS - verified_count, 0)
        own_verified = sum(
            _artist(item).casefold() == seed_key for item in verified_items
        )
        related_verified = verified_count - own_verified
        own = [item for item in unresolved if _artist(item).casefold() == seed_key]
        related = [item for item in unresolved if _artist(item).casefold() != seed_key]
        for group in (own, related):
            group.sort(
                key=lambda item: verification_rank(item, _recording_key(item))
                or (1, 0.0, 0.0, _recording_key(item))
            )
        own_needed = max(8 - own_verified, 0)
        related_needed = max(4 - related_verified, 0)
        preferred = [
            *own[:own_needed],
            *related[:related_needed],
            *own[own_needed:],
            *related[related_needed:],
        ]
        ordered_radio_groups.append(
            (
                1 if gap == 0 else 0,
                gap if gap else max(POPULAR_RADIO_CARD_TARGET_TRACKS - verified_count, 0),
                radio_group_order[seed_key],
                preferred,
            )
        )
    ordered_radio_groups.sort(key=lambda value: (value[0], value[1], value[2]))
    for _complete, _gap, _order, items in ordered_radio_groups:
        for item in items:
            key = _recording_key(item)
            rank = verification_rank(item, key)
            if key and rank is not None:
                radio_queue.append((rank, key, item))
    if radio_queue:
        queues["radio_artist_catalog"] = radio_queue

    special_pools = {"album", "radio_artist_catalog"}
    core_pools = sorted(
        (pool for pool in queues if pool not in special_pools),
        key=lambda pool: (_POOL_PRIORITY.get(pool, 8), pool),
    )
    positions = {pool: 0 for pool in queues}
    output: List[Tuple[str, str, Dict[str, Any]]] = []
    seen: set[str] = set()
    safe_limit = max(int(limit), 0)

    def append_next(pool: str) -> bool:
        queue = queues.get(pool) or []
        position = positions.get(pool, 0)
        while position < len(queue):
            _rank, key, item = queue[position]
            position += 1
            positions[pool] = position
            if key in seen:
                continue
            seen.add(key)
            output.append((pool, key, item))
            return True
        return False

    reserve_per_special = min(
        16,
        # Radio qualification and complete-album conversion both need enough
        # verified tracks to cross their publish threshold in one pass. An
        # eight-item reserve stranded both pools just below completion.
        max(POPULAR_RADIO_CARD_TARGET_TRACKS, safe_limit // 6),
    )
    for _index in range(reserve_per_special):
        if len(output) >= safe_limit:
            break
        append_next("radio_artist_catalog")
        if len(output) >= safe_limit:
            break
        append_next("album")

    while len(output) < safe_limit:
        progressed = False
        for pool in core_pools:
            if len(output) >= safe_limit:
                break
            if append_next(pool):
                progressed = True
        if not progressed:
            for pool in ("radio_artist_catalog", "album", *core_pools):
                if len(output) >= safe_limit:
                    break
                if append_next(pool):
                    progressed = True
            if not progressed:
                break
    return output


def _adaptive_verification_limit(unresolved_count: int) -> int:
    unresolved = max(int(unresolved_count or 0), 0)
    if unresolved <= _MIN_ADAPTIVE_VERIFICATIONS:
        return unresolved
    proportional = (unresolved + 2) // 3
    return min(
        unresolved,
        max(
            _MIN_ADAPTIVE_VERIFICATIONS,
            min(_MAX_ADAPTIVE_VERIFICATIONS, proportional),
        ),
    )


def verify_materialized_supply(
    server: Any,
    supply: MaterializedCandidateSupply,
    taste: TasteProfile,
    *,
    max_new_verifications: int | None = None,
    max_workers: int = _MAX_VERIFICATION_WORKERS,
    time_budget_seconds: float | None = None,
    hydrate_artist_artwork: bool = True,
) -> MaterializedCandidateSupply:
    started = time.perf_counter()
    deadline = (
        started + max(float(time_budget_seconds), 0.05)
        if time_budget_seconds is not None
        else None
    )

    def remaining_seconds() -> float | None:
        if deadline is None:
            return None
        return max(deadline - time.perf_counter(), 0.0)

    all_items: List[Dict[str, Any]] = []
    for pool, items in supply.pools.items():
        if pool == "album":
            for album in items or []:
                all_items.extend(
                    track for track in album.get("canonical_tracks") or [] if isinstance(track, dict)
                )
        else:
            all_items.extend(item for item in items or [] if isinstance(item, dict))
    entity_keys = [_recording_key(item) for item in all_items]
    item_by_key = {
        _recording_key(item): item
        for item in all_items
        if _recording_key(item)
    }
    stored_by_key = _load_sources(server, entity_keys)
    verified_by_key = {
        key: source
        for key, sources in stored_by_key.items()
        if (source := _verified_source(sources, item_by_key.get(key))) is not None
    }
    registry_hits = len(verified_by_key)
    verified_new = 0
    unavailable = 0
    blocked_failures = 0
    format_failures = 0
    attempted = 0

    unresolved_count = len(set(entity_keys) - set(verified_by_key) - {""})
    verification_limit = (
        _adaptive_verification_limit(unresolved_count)
        if max_new_verifications is None
        else min(max(int(max_new_verifications), 0), unresolved_count)
    )
    scan_limit = min(unresolved_count, max(verification_limit * 2, verification_limit))
    preverification_rejections: Dict[str, int] = {}
    shortlist_now = time.time()
    shortlist = _shortlist(
        supply,
        taste,
        verified_by_key,
        limit=scan_limit,
        rejection_counts=preverification_rejections,
        source_registry_by_key=stored_by_key,
        now=shortlist_now,
    )
    attempted_by_pool: Dict[str, int] = {}
    attempted_by_authority: Dict[str, int] = {}
    verified_by_pool: Dict[str, int] = {}
    source_lookup_misses = 0
    source_lookup_deferred = preverification_rejections.get("lookup_cooldown", 0)
    timed_out_tasks = 0
    if not _provider_is_blocked(server):
        pending_tasks: List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]] = []
        lookup_tasks: List[Tuple[str, str, Dict[str, Any], set[str]]] = []
        now = time.time()
        for pool, entity_key, item in shortlist:
            if len(pending_tasks) >= verification_limit:
                break
            known_sources = stored_by_key.get(entity_key) or []
            recording = CanonicalRecording.from_item(item)
            candidates = sorted(
                [
                    matched
                    for source in known_sources
                    if source.get("provider") == "youtube"
                    and source.get("verification_state") == "pending"
                    and _YOUTUBE_ID_RE.fullmatch(_text(source.get("source_id")))
                    and (matched := _rematch_pending_source(recording, source)) is not None
                ],
                key=_source_rank,
                reverse=True,
            )
            rejected_ids = {
                _text(source.get("source_id"))
                for source in known_sources
                if source.get("verification_state") == "unavailable"
                or (
                    source.get("verification_state") == "temporarily_blocked"
                    and float(source.get("retry_at") or 0.0) > now
                )
            }
            candidate = next(
                (source for source in candidates if _text(source.get("source_id")) not in rejected_ids),
                None,
            )
            if candidate:
                pending_tasks.append((pool, entity_key, item, candidate))
            elif _lookup_is_deferred(known_sources, now=now):
                source_lookup_deferred += 1
            else:
                lookup_tasks.append((pool, entity_key, item, rejected_ids))

        worker_count = max(1, min(int(max_workers), _MAX_VERIFICATION_WORKERS))
        if lookup_tasks and len(pending_tasks) < verification_limit:
            executor = ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="auralis-source-lookup",
            )
            lookup_futures = {}
            try:
                lookup_cursor = 0
                lookup_wave_size = worker_count * 2
                while (
                    lookup_cursor < len(lookup_tasks)
                    and len(pending_tasks) < verification_limit
                ):
                    remaining = remaining_seconds()
                    if remaining is not None and remaining <= 0:
                        break
                    wave = lookup_tasks[
                        lookup_cursor : lookup_cursor + lookup_wave_size
                    ]
                    lookup_cursor += len(wave)
                    lookup_futures = {
                        executor.submit(_enumerate_youtube_sources, server, item): (
                            pool,
                            entity_key,
                            item,
                            rejected_ids,
                        )
                        for pool, entity_key, item, rejected_ids in wave
                    }
                    lookup_results: Dict[
                        str,
                        Tuple[str, Dict[str, Any], set[str], List[Dict[str, Any]]],
                    ] = {}
                    wave_timed_out = False
                    try:
                        for future in as_completed(
                            lookup_futures,
                            timeout=(
                                max(remaining_seconds() or 0.0, 0.001)
                                if deadline is not None
                                else None
                            ),
                        ):
                            pool, entity_key, item, rejected_ids = lookup_futures[
                                future
                            ]
                            try:
                                candidates = list(future.result() or [])
                            except Exception:
                                candidates = []
                            lookup_results[entity_key] = (
                                pool,
                                item,
                                rejected_ids,
                                candidates,
                            )
                    except FuturesTimeoutError:
                        wave_timed_out = True
                        timed_out_tasks += sum(
                            not future.done() for future in lookup_futures
                        )
                    discovered_sources = [
                        (entity_key, source)
                        for entity_key, (
                            _pool,
                            _item,
                            _rejected,
                            candidates,
                        ) in lookup_results.items()
                        for source in candidates
                    ]
                    discovered_sources.extend(
                        (entity_key, _lookup_marker(candidates))
                        for entity_key, (
                            _pool,
                            _item,
                            _rejected,
                            candidates,
                        ) in lookup_results.items()
                    )
                    _store_sources(
                        server,
                        discovered_sources,
                        store_initialized=True,
                    )
                    for pool, entity_key, item, _rejected_ids in wave:
                        if len(pending_tasks) >= verification_limit:
                            break
                        result = lookup_results.get(entity_key)
                        if result is None:
                            source_lookup_misses += 1
                            continue
                        _result_pool, _result_item, rejected_ids, candidates = result
                        candidate = next(
                            (
                                source
                                for source in candidates
                                if _text(source.get("source_id")) not in rejected_ids
                            ),
                            None,
                        )
                        if candidate is None:
                            source_lookup_misses += 1
                            continue
                        pending_tasks.append((pool, entity_key, item, candidate))
                    if wave_timed_out:
                        break
            finally:
                for future in lookup_futures:
                    if not future.done():
                        future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
        executor = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="auralis-source-verify",
        )
        futures = {}
        try:
            for start in range(0, min(len(pending_tasks), verification_limit), worker_count):
                remaining = remaining_seconds()
                if remaining is not None and remaining <= 0:
                    break
                batch = pending_tasks[start : start + worker_count]
                futures = {
                    executor.submit(_verify_source, server, entity_key, source): (
                        pool,
                        entity_key,
                        _text(source.get("authority")) or "unknown",
                    )
                    for pool, entity_key, _item, source in batch
                }
                completed_sources: List[Tuple[str, Dict[str, Any]]] = []
                batch_timed_out = False
                try:
                    for future in as_completed(
                        futures,
                        timeout=(
                            max(remaining_seconds() or 0.0, 0.001)
                            if deadline is not None
                            else None
                        ),
                    ):
                        pool, entity_key, source_authority = futures[future]
                        attempted += 1
                        attempted_by_pool[pool] = attempted_by_pool.get(pool, 0) + 1
                        attempted_by_authority[source_authority] = (
                            attempted_by_authority.get(source_authority, 0) + 1
                        )
                        try:
                            source, outcome = future.result()
                        except Exception:
                            source, outcome = None, "unavailable"
                        if source is not None:
                            completed_sources.append((entity_key, source))
                        if source is not None and outcome in {
                            "verified_cache",
                            "verified_resolver",
                        }:
                            verified_by_key[entity_key] = source
                            verified_new += 1
                            verified_by_pool[pool] = (
                                verified_by_pool.get(pool, 0) + 1
                            )
                        elif outcome == "source_blocked":
                            blocked_failures += 1
                        elif outcome == "format_unavailable":
                            format_failures += 1
                            unavailable += 1
                        else:
                            unavailable += 1
                except FuturesTimeoutError:
                    batch_timed_out = True
                    timed_out_tasks += sum(not future.done() for future in futures)
                _store_sources(
                    server,
                    completed_sources,
                    store_initialized=True,
                )
                if (
                    blocked_failures >= 2
                    or format_failures >= _FORMAT_FAILURE_CIRCUIT_THRESHOLD
                ):
                    break
                if batch_timed_out:
                    break
        finally:
            for future in futures:
                if not future.done():
                    future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
        if blocked_failures >= 2:
            _set_provider_health(server, blocked=True, failures=blocked_failures)
        elif format_failures >= _FORMAT_FAILURE_CIRCUIT_THRESHOLD:
            _set_provider_health(
                server,
                blocked=True,
                failures=format_failures,
                reason="format_unavailable",
            )
        elif attempted and not blocked_failures:
            _set_provider_health(server, blocked=False)

    radio_artwork_items = [
        item
        for item in supply.pools.get("radio_artist_catalog", []) or []
        if isinstance(item, dict)
    ]
    artwork_items = [*radio_artwork_items, *all_items]
    artwork_item_by_entity: Dict[str, Dict[str, Any]] = {}
    for item in artwork_items:
        entity_key = _recording_key(item)
        if entity_key:
            artwork_item_by_entity.setdefault(entity_key, item)
    missing_artwork_sources = []
    seen_artwork_entities: set[str] = set()
    for item in artwork_items:
        entity_key = _recording_key(item)
        source = verified_by_key.get(entity_key)
        if (
            not entity_key
            or source is None
            or _text(source.get("artist_thumbnail"))
            or entity_key in seen_artwork_entities
        ):
            continue
        if _artist_browse_ids(source) or (
            _text(source.get("channel_id"))
            and _text(source.get("authority")).casefold()
            in {"official_artist_channel", "topic", "vevo"}
        ):
            missing_artwork_sources.append((entity_key, source))
            seen_artwork_entities.add(entity_key)
    # Home supply owns artwork as an independent job. A bounded source pass
    # must never spend its remaining wall clock on channel artwork hydration.
    if missing_artwork_sources and hydrate_artist_artwork:
        artwork_source_ids: List[str] = []
        for _entity_key, source in missing_artwork_sources:
            artwork_source_ids.extend(_artist_browse_ids(source))
            if _text(source.get("authority")).casefold() in {
                "official_artist_channel",
                "topic",
                "vevo",
            }:
                artwork_source_ids.append(_text(source.get("channel_id")))
        try:
            channel_artwork = _youtube_channel_thumbnails(
                server,
                artwork_source_ids,
            )
        except Exception:
            channel_artwork = {}
        artwork_updates: List[Tuple[str, Dict[str, Any]]] = []
        artwork_by_artist: Dict[str, str] = {}
        for entity_key, source in verified_by_key.items():
            existing_thumbnail = _text(source.get("artist_thumbnail"))
            item = artwork_item_by_entity.get(entity_key) or {}
            artist_key = normalize_artist_name(
                _artist(item) or source.get("catalog_artist")
            )
            if artist_key and existing_thumbnail:
                artwork_by_artist.setdefault(artist_key, existing_thumbnail)
        for entity_key, source in missing_artwork_sources:
            thumbnail = next(
                (
                    channel_artwork[source_id]
                    for source_id in _artist_browse_ids(source)
                    if channel_artwork.get(source_id)
                ),
                "",
            )
            if (
                not thumbnail
                and _text(source.get("authority")).casefold()
                in {"official_artist_channel", "topic", "vevo"}
            ):
                thumbnail = channel_artwork.get(_text(source.get("channel_id")), "")
            if not thumbnail:
                item = artwork_item_by_entity.get(entity_key) or {}
                artist_key = normalize_artist_name(
                    _artist(item) or source.get("catalog_artist")
                )
                thumbnail = artwork_by_artist.get(artist_key, "")
            if not thumbnail:
                continue
            source["artist_thumbnail"] = thumbnail
            item = artwork_item_by_entity.get(entity_key) or {}
            artist_key = normalize_artist_name(
                _artist(item) or source.get("catalog_artist")
            )
            if artist_key:
                artwork_by_artist.setdefault(artist_key, thumbnail)
            artwork_updates.append((entity_key, source))
        updated_keys = {entity_key for entity_key, _source in artwork_updates}
        for entity_key, source in missing_artwork_sources:
            if entity_key in updated_keys:
                continue
            item = artwork_item_by_entity.get(entity_key) or {}
            artist_key = normalize_artist_name(
                _artist(item) or source.get("catalog_artist")
            )
            thumbnail = artwork_by_artist.get(artist_key, "")
            if not thumbnail:
                continue
            source["artist_thumbnail"] = thumbnail
            artwork_updates.append((entity_key, source))
        _store_sources(server, artwork_updates, store_initialized=True)

    pools: Dict[str, List[Dict[str, Any]]] = {}
    for pool, items in supply.pools.items():
        if pool != "album":
            pools[pool] = [
                _apply_source(item, source)
                for item in items or []
                if isinstance(item, dict)
                and (source := verified_by_key.get(_recording_key(item))) is not None
            ]
            continue
        albums: List[Dict[str, Any]] = []
        for album in items or []:
            canonical_tracks = [
                item for item in album.get("canonical_tracks") or [] if isinstance(item, dict)
            ]
            playable_tracks = [
                _apply_source(item, source)
                for item in canonical_tracks
                if (source := verified_by_key.get(_recording_key(item))) is not None
            ]
            required = len(canonical_tracks) if len(canonical_tracks) < 6 else max(6, int(len(canonical_tracks) * 0.8 + 0.999))
            album_thumbnail = _text(album.get("thumbnail") or album.get("image"))
            if not album_thumbnail:
                album_thumbnail = next(
                    (
                        _text(track.get("thumbnail") or track.get("image"))
                        for track in playable_tracks
                        if _text(track.get("thumbnail") or track.get("image"))
                    ),
                    "",
                )
            albums.append(
                {
                    **dict(album),
                    "canonical_tracks": playable_tracks,
                    "tracks": playable_tracks,
                    "track_count": len(playable_tracks),
                    "canonical_track_count": len(canonical_tracks),
                    "playable_coverage": round(len(playable_tracks) / max(len(canonical_tracks), 1), 4),
                    "playable": bool(canonical_tracks and len(playable_tracks) >= required),
                    "thumbnail": album_thumbnail,
                    "image": album_thumbnail,
                }
            )
        pools[pool] = albums

    diagnostics = {
        **dict(supply.diagnostics or {}),
        "source_registry_candidates": len(set(entity_keys) - {""}),
        "source_registry_hits": registry_hits,
        "source_verification_limit": verification_limit,
        "source_verification_shortlisted": len(shortlist),
        "source_preverification_rejections": preverification_rejections,
        "source_lookup_misses": source_lookup_misses,
        "source_lookup_deferred": source_lookup_deferred,
        "source_verification_timed_out": timed_out_tasks,
        "source_verification_attempted": attempted,
        "source_verification_verified": verified_new,
        "source_verification_attempted_by_pool": attempted_by_pool,
        "source_verification_attempted_by_authority": attempted_by_authority,
        "source_verification_verified_by_pool": verified_by_pool,
        "source_verification_unavailable": unavailable,
        "source_verification_format_unavailable": format_failures,
        "source_verification_blocked": blocked_failures,
        "source_verification_circuit_open": _provider_is_blocked(server),
        "source_verification_elapsed_ms": int((time.perf_counter() - started) * 1000),
    }
    print(
        "[EBB:source-registry] "
        f"candidates={diagnostics['source_registry_candidates']} "
        f"hits={registry_hits} limit={verification_limit} "
        f"shortlisted={len(shortlist)} lookup_misses={source_lookup_misses} "
        f"lookup_deferred={source_lookup_deferred} "
        f"timed_out={timed_out_tasks} "
        f"prefiltered={sum(preverification_rejections.values())} "
        f"attempted={attempted} verified={verified_new} "
        f"unavailable={unavailable} format_unavailable={format_failures} blocked={blocked_failures} "
        f"pools={','.join(f'{pool}:{count}' for pool, count in attempted_by_pool.items()) or '-'} "
        f"authority={','.join(f'{authority}:{count}' for authority, count in attempted_by_authority.items()) or '-'} "
        f"circuit_open={int(bool(diagnostics['source_verification_circuit_open']))} "
        f"elapsed_ms={diagnostics['source_verification_elapsed_ms']}"
    )
    return replace(supply, pools=pools, diagnostics=diagnostics)
