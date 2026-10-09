from __future__ import annotations

import hashlib
import json
import time
from threading import Lock
from typing import Any

from fastapi import HTTPException

from .stream_core_runtime import (
    prepare_streams_with_failures,
    summarize_prepare_metrics,
)
from ..details.detail_runtime import (
    build_album_details_payload,
    build_artist_details_payload,
    build_track_details_payload,
)
from ..lyrics import LyricsService
from ..storage.cache_runtime import (
    detail_result_cache, detail_result_cache_lock, lookup_ttl_cache, store_ttl_cache,
)

# Bounded striped locks coalesce a seed's requests without retaining futures.
_related_locks = [Lock() for _ in range(32)]


def _related_watch(server: Any, source_id: str, depth: int):
    key = f"{id(server.ytmusic)}:{source_id}"
    with _related_locks[hash(key) % len(_related_locks)]:
        cached = lookup_ttl_cache(detail_result_cache, detail_result_cache_lock, "related", key)
        if cached and cached["depth"] >= depth:
            return cached["watch"]
        watch = server.ytmusic.get_watch_playlist(
            videoId=source_id, radio=True, limit=depth,
        ) or {}
        # Empty/transient results must remain retryable; exclude per-user filters.
        if watch.get("tracks"):
            with detail_result_cache_lock:
                entries = detail_result_cache.setdefault("related", {})
                for old in list(entries)[:max(0, len(entries) - 255)]:
                    entries.pop(old, None)
            store_ttl_cache(detail_result_cache, detail_result_cache_lock, "related", key,
                            {"depth": depth, "watch": watch}, 300)
        return watch


class MediaService:
    def __init__(self, server: Any) -> None:
        self._server = server

    def health_check(self):
        return health_check(self._server)

    def latency_summary(self):
        return latency_summary(self._server)

    def prepare_session(self, req: Any):
        return prepare_session(self._server, req)

    def get_track_details(self, req: Any):
        return get_track_details(self._server, req)

    def get_related_tracks(self, req: Any):
        return get_related_tracks(self._server, req)

    def get_track_lyrics(
        self,
        track_key: str,
        *,
        title: str = "",
        artist: str = "",
        album: str = "",
        duration: int | None = None,
        source_id: str = "",
        force_refresh: bool = False,
    ):
        return get_track_lyrics(
            self._server,
            track_key,
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            source_id=source_id,
            force_refresh=force_refresh,
        )

    def get_track_lyrics_meaning(self, video_id: str, req: Any):
        return get_track_lyrics_meaning(self._server, video_id, req)

    def get_album_details(self, album_id: str):
        return get_album_details(self._server, album_id)

    def get_artist_details(self, artist_id: str):
        return get_artist_details(self._server, artist_id)


def prepare_session(server: Any, req: Any):
    start_time = time.perf_counter()
    if req.background:
        from ..discovery.source_registry import youtube_background_resolution_blocked

        if youtube_background_resolution_blocked(server):
            return {
                "status": "success",
                "prepared": {},
                "prepared_ids": [],
                "failed": {},
                "failed_ids": [],
                "background_skipped": True,
                "skip_reason": "youtube_temporarily_blocked",
                "summary": summarize_prepare_metrics(server),
                "server_ms": int((time.perf_counter() - start_time) * 1000),
            }
    requested_limit = max(req.lookahead or len(req.track_keys), 1)
    if req.background:
        requested_limit = min(
            requested_limit,
            int(getattr(server, "PREPARE_BACKGROUND_MAX_LOOKAHEAD", 3)),
        )
    limit = min(requested_limit, server.PREPARE_SESSION_MAX_LOOKAHEAD)
    from .stream_runtime import prepare_playback_tracks

    prepared, failed = prepare_playback_tracks(
        server,
        req.track_keys,
        limit=limit,
        current_track_key=req.current_track_key,
        active_queue=req.active_queue,
        defer_all_chunks=req.background,
    )
    if getattr(req, 'cache_full', False):
        from .stream_core_runtime import cache_prepared_track
        for value in list(prepared.values())[:1]:
            if value.get('provider') == 'youtube':
                value['fully_cached'] = cache_prepared_track(server, value['source_id'])
    if req.background and failed:
        from ..discovery.source_registry import record_youtube_background_failures

        record_youtube_background_failures(server, failed)
    print(f'[EBB:prepare] tracks={",".join(req.track_keys[:limit])} '
          f'ms={int((time.perf_counter()-start_time)*1000)} '
          f'prepared={len(prepared)} failed={len(failed)} '
          f'fullyCached={sum(v.get("fully_cached") is True for v in prepared.values())}', flush=True)
    return {
        "status": "success",
        "prepared": prepared,
        "prepared_ids": list(prepared.keys()),
        "failed": failed,
        "failed_ids": list(failed.keys()),
        "background_skipped": False,
        "summary": summarize_prepare_metrics(server),
        "server_ms": int((time.perf_counter() - start_time) * 1000),
    }


def latency_summary(server: Any):
    from ..storage.postgres import request_log_stats
    return {
        "status": "success",
        "summary": summarize_prepare_metrics(server),
        "stream_info_cache_size": len(server.stream_info_cache),
        "stream_chunk_cache_size": len(server.stream_chunk_cache),
        "request_diagnostics": request_log_stats(),
    }


def health_check(_server: Any):
    return {"status": "Auralis Python Proxy is running"}


def get_track_details(server: Any, req: Any):
    try:
        return build_track_details_payload(server, req.video_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def get_related_tracks(server: Any, req: Any):
    """Resolve a canonical seed once, then page its provider radio results."""

    from .stream_runtime import playback_source_candidates

    track_key = str(req.track_key or "").strip()
    sources = playback_source_candidates(server, track_key)
    if not sources and track_key.startswith(
        ("recording:", "musicbrainz:recording:")
    ):
        from ..discovery.source_registry import (
            resolve_user_requested_playback_source,
        )

        try:
            resolved = resolve_user_requested_playback_source(
                server,
                track_key=track_key,
                title=str(getattr(req, "title", "") or ""),
                artist=str(getattr(req, "artist", "") or ""),
                album=str(getattr(req, "album", "") or ""),
                duration=int(getattr(req, "duration", 0) or 0),
                source_id=str(getattr(req, "source_id", "") or ""),
                source_provider=str(
                    getattr(req, "source_provider", "youtube") or "youtube"
                ),
            )
        except Exception as exc:
            print(
                "[EBB:related-tracks] "
                f"track={track_key} outcome=source_resolution_failed "
                f"error={type(exc).__name__}"
            )
            resolved = None
        if resolved is not None:
            sources = [resolved]
    source_id = next(
        (
            str(source.get("source_id") or "").strip()
            for source in sources
            if source.get("provider") == "youtube" and source.get("source_id")
        ),
        "",
    )
    if not source_id and len(track_key) == 11 and ":" not in track_key:
        source_id = track_key
    if not source_id:
        return {
            "status": "temporarily_unavailable",
            "tracks": [],
            "next_cursor": str(max(int(req.offset), 0)),
            "has_more": True,
        }
    try:
        # Fetch beyond tracks the client already owns; the provider defaults to
        # a short first page, which makes repeated retries return only duplicates.
        depth = min(300, max(50, len(req.avoid_ids) + int(req.offset) + int(req.limit) + 1))
        watch = _related_watch(server, source_id, depth)
    except Exception as exc:
        print(f"[EBB:related-tracks] source={source_id} provider_error={type(exc).__name__}")
        return {
            "status": "temporarily_unavailable",
            "tracks": [],
            "next_cursor": str(max(int(req.offset), 0)),
            "has_more": True,
        }
    avoid = {str(value or "").strip() for value in req.avoid_ids if str(value or "").strip()}
    avoid.update({track_key, source_id})
    candidates = []
    seen = set(avoid)
    for entry in watch.get("tracks") or []:
        if not isinstance(entry, dict):
            continue
        video_id = str(entry.get("videoId") or entry.get("id") or "").strip()
        if not video_id or video_id in seen:
            continue
        title = str(entry.get("title") or "").strip()
        artist = server.extract_artist(entry)
        if not title or not artist:
            continue
        seen.add(video_id)
        album = server.extract_album_info(entry) or {}
        candidates.append({
            "id": video_id,
            "track_key": video_id,
            "videoId": video_id,
            "title": title,
            "channel": artist,
            "artist": artist,
            "duration": server.parse_duration_seconds(
                entry.get("length") or entry.get("duration_seconds") or entry.get("duration")
            ),
            "thumbnail": server.extract_thumbnail(entry),
            "album": album.get("title") or album.get("name") or "",
            "album_id": album.get("id") or album.get("browseId") or "",
            "playback": {
                "provider": "youtube",
                "source_id": video_id,
                "authority": "ytmusic_radio",
            },
        })
    offset = max(int(req.offset), 0)
    limit = min(max(int(req.limit), 1), 30)
    discovery_session = str(getattr(req, "discovery_session", "") or "").strip()[:128]
    if discovery_session:
        recent = {str(value or "").strip() for value in getattr(req, "recent_ids", [])[:100]}
        seed = str(track_key or source_id)
        provider_positions = {item["id"]: index for index, item in enumerate(candidates)}

        def discovery_order(item):
            identity = item["id"]
            # Preserve provider relevance bands while varying order inside each
            # band. Recent tracks stay eligible and fall behind fresh tracks.
            band = provider_positions[identity] // 12
            digest = hashlib.sha256(
                f"{seed}\0{discovery_session}\0{identity}".encode("utf-8")
            ).hexdigest()
            return band, identity in recent, digest

        candidates.sort(key=discovery_order)
    page = candidates[offset : offset + limit]
    next_offset = offset + len(page)
    return {
        "status": "ready" if page else "exhausted",
        "tracks": page,
        "next_cursor": str(next_offset),
        "has_more": next_offset < len(candidates),
        "source_id": source_id,
    }


def _lyrics_service(server: Any) -> LyricsService:
    service = getattr(server, "lyrics_service", None)
    if isinstance(service, LyricsService):
        return service
    service = LyricsService(server)
    setattr(server, "lyrics_service", service)
    return service


def get_track_lyrics(
    server: Any,
    track_key: str,
    *,
    title: str = "",
    artist: str = "",
    album: str = "",
    duration: int | None = None,
    source_id: str = "",
    force_refresh: bool = False,
):
    started = time.perf_counter()
    result = _lyrics_service(server).get(
        track_key,
        title=title,
        artist=artist,
        album=album,
        duration=duration,
        source_id=source_id,
        force_refresh=force_refresh,
    )
    attempts = ",".join(
        f"{item.get('provider')}:{item.get('outcome')}"
        for item in result.get("provider_attempts") or []
        if isinstance(item, dict)
    )
    identity_kind = "canonical" if ":" in str(track_key or "") else "source"
    print(
        "[EBB:lyrics] "
        f"identity={identity_kind} status={result.get('lyrics_status')} "
        f"source={result.get('source') or '-'} "
        f"resolved={bool(result.get('resolved_source_id'))} "
        f"lines={len(result.get('lines') or [])} "
        f"cache={bool(result.get('cache_hit'))} "
        f"attempts={attempts or '-'} "
        f"elapsed_ms={int((time.perf_counter() - started) * 1000)}"
    )
    return result


def _lyrics_meaning_cache(server: Any) -> dict[str, dict[str, Any]]:
    cache = getattr(server, "lyrics_meaning_cache", None)
    if isinstance(cache, dict):
        return cache
    cache = {}
    setattr(server, "lyrics_meaning_cache", cache)
    return cache


def _trim_text(value: Any, limit: int = 220) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _request_lines(req: Any) -> list[dict[str, Any]]:
    raw_lines = getattr(req, "lines", None) or []
    output: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_lines):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        output.append({
            "index": int(raw.get("index") or index),
            "text": text,
        })
    return output


def _lyrics_hash(lines: list[dict[str, Any]]) -> str:
    text = "\n".join(str(line.get("text") or "").strip() for line in lines)
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _lyrics_context_text(lines: list[dict[str, Any]], *, max_chars: int = 3600) -> str:
    text = "\n".join(str(line.get("text") or "").strip() for line in lines)
    text = "\n".join(line for line in text.splitlines() if line.strip())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit("\n", 1)[0].strip()


def _safe_string_list(value: Any, *, limit: int = 5) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        text = _trim_text(item, 180)
        if text:
            output.append(text)
        if len(output) >= limit:
            break
    return output


def _fallback_meaning_payload(
    *,
    video_id: str,
    title: str,
    artist: str,
    has_lyrics: bool,
    lyrics_hash: str,
) -> dict[str, Any]:
    track_label = " by ".join(part for part in (title or "this song", artist) if part)
    basis = "available lyrics and track metadata" if has_lyrics else "track metadata only"
    summary = (
        f"This is a quick context-based read of {track_label}. "
        "Neatie could not reach the full interpretation model, so treat this as a light starting point."
    )
    seed = (
        f"Let's discuss the meaning of {title or 'this song'}"
        f"{f' by {artist}' if artist else ''}. Start from the quick interpretation and help me go deeper."
    )
    return {
        "status": "success",
        "video_id": video_id,
        "cached": False,
        "summary": summary,
        "themes": ["Mood", "Story", "Personal interpretation"],
        "emotional_tone": "Uncertain, based on limited context.",
        "context_notes": [f"Interpretation basis: {basis}."],
        "notable_imagery": [],
        "confidence": 0.35 if not has_lyrics else 0.5,
        "source_notes": (
            "Generated without long lyric quotes. The model fallback did not use external context."
        ),
        "assistant_seed_message": seed,
        "lyrics_hash": lyrics_hash,
    }


def _meaning_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "themes": {"type": "array", "items": {"type": "string"}},
            "emotional_tone": {"type": "string"},
            "context_notes": {"type": "array", "items": {"type": "string"}},
            "notable_imagery": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
            "source_notes": {"type": "string"},
        },
        "required": [
            "summary",
            "themes",
            "emotional_tone",
            "context_notes",
            "notable_imagery",
            "confidence",
            "source_notes",
        ],
    }


def get_track_lyrics_meaning(server: Any, video_id: str, req: Any):
    requested_id = (video_id or getattr(req, "video_id", "") or "").strip()
    if not requested_id:
        raise HTTPException(status_code=400, detail="video_id is required")
    title = _trim_text(getattr(req, "title", ""), 180)
    artist = _trim_text(getattr(req, "artist", ""), 180)
    album = _trim_text(getattr(req, "album", ""), 180)
    year = _trim_text(getattr(req, "year", ""), 24)
    source = _trim_text(getattr(req, "source", ""), 120)
    lines = _request_lines(req)

    if not lines:
        try:
            lyrics_payload = get_track_lyrics(
                server,
                requested_id,
                title=title,
                artist=artist,
                album=album,
            )
            lines = list(lyrics_payload.get("lines") or [])
            source = source or _trim_text(lyrics_payload.get("source"), 120)
        except Exception:
            lines = []

    lyrics_hash = _lyrics_hash(lines)
    cache_key = hashlib.sha256(
        json.dumps(
            {
                "video_id": requested_id,
                "title": title,
                "artist": artist,
                "lyrics_hash": lyrics_hash,
            },
            sort_keys=True,
        ).encode("utf-8", errors="ignore")
    ).hexdigest()
    cache = _lyrics_meaning_cache(server)
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        result = dict(cached)
        result["cached"] = True
        return result

    has_lyrics = bool(lines)
    lyrics_context = _lyrics_context_text(lines)
    metadata = ", ".join(
        part
        for part in (
            f"title: {title}" if title else "",
            f"artist: {artist}" if artist else "",
            f"album: {album}" if album else "",
            f"year: {year}" if year else "",
            f"lyrics source: {source}" if source else "",
        )
        if part
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are Neatie's Behind the Lyrics interpreter. "
                "Give a concise, careful interpretation of a song. "
                "Do not quote more than a few words from lyrics. "
                "Do not invent artist biography, historical events, or song intent as fact. "
                "Label uncertainty when context is limited. "
                "Return only JSON matching the schema."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Song metadata: {metadata or 'unknown'}.\n"
                f"Lyrics available: {'yes' if has_lyrics else 'no'}.\n"
                "Interpret the song for a listener who wants to understand the mood, themes, "
                "possible story, and imagery. Keep it brief and grounded.\n\n"
                f"Lyrics context:\n{lyrics_context or '[No lyrics available]'}"
            ),
        },
    ]

    try:
        call_structured = getattr(server, "_call_ollama_structured", None)
        if not callable(call_structured):
            raise RuntimeError("structured assistant runtime unavailable")
        raw = call_structured(
            messages,
            schema=_meaning_schema(),
            temperature=0.35,
            timeout_seconds=45,
            model_override=getattr(server, "OLLAMA_FAST_MODEL", None),
        )
        confidence = raw.get("confidence")
        try:
            confidence_value = float(confidence)
        except Exception:
            confidence_value = 0.68 if has_lyrics else 0.42
        confidence_value = max(0.0, min(1.0, confidence_value))
        seed = (
            f"I was listening to {title or 'this song'}"
            f"{f' by {artist}' if artist else ''}. "
            "Here is Neatie's quick interpretation:\n\n"
            f"{_trim_text(raw.get('summary'), 800)}\n\n"
            "Can we talk more about what this song might mean?"
        )
        result = {
            "status": "success",
            "video_id": requested_id,
            "cached": False,
            "summary": _trim_text(raw.get("summary"), 900),
            "themes": _safe_string_list(raw.get("themes"), limit=6),
            "emotional_tone": _trim_text(raw.get("emotional_tone"), 240),
            "context_notes": _safe_string_list(raw.get("context_notes"), limit=5),
            "notable_imagery": _safe_string_list(raw.get("notable_imagery"), limit=5),
            "confidence": confidence_value,
            "source_notes": _trim_text(raw.get("source_notes"), 260),
            "assistant_seed_message": seed,
            "lyrics_hash": lyrics_hash,
        }
    except Exception:
        result = _fallback_meaning_payload(
            video_id=requested_id,
            title=title,
            artist=artist,
            has_lyrics=has_lyrics,
            lyrics_hash=lyrics_hash,
        )

    cache[cache_key] = dict(result)
    if len(cache) > 256:
        for key in list(cache.keys())[:64]:
            cache.pop(key, None)
    return result


def get_album_details(server: Any, album_id: str):
    try:
        normalized_id = str(album_id or "").strip()
        from ..recommend.store_runtime import open_recommendation_store_connection

        aliases = {normalized_id}
        if normalized_id.startswith("ytmusic:album:"):
            aliases.add(normalized_id.removeprefix("ytmusic:album:"))
        if normalized_id.startswith("musicbrainz:release-group:"):
            token = normalized_id.removeprefix("musicbrainz:release-group:")
            aliases.update({f"musicbrainz:release:{token}", token})
        connection = open_recommendation_store_connection(server)
        try:
            placeholders = ",".join("?" for _ in aliases)
            rows = connection.execute(
                f"""
                SELECT payload_json
                FROM catalog_entities
                WHERE entity_type = 'album' AND entity_key IN ({placeholders})
                ORDER BY updated_at DESC
                """,
                list(aliases),
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            payload = json.loads(str(row[0] or "{}"))
            raw_tracks = payload.get("tracks") or payload.get("canonical_tracks") or []
            tracks = [
                dict(item)
                for item in raw_tracks
                if isinstance(item, dict) and item.get("playable") is not False
            ]
            if tracks:
                return {
                    **payload,
                    "status": "success",
                    "id": normalized_id,
                    "track_count": len(tracks),
                    "tracks": tracks,
                }
        provider_id = normalized_id.removeprefix("ytmusic:album:")
        if normalized_id.startswith("musicbrainz:"):
            raise HTTPException(status_code=404, detail="Canonical album is not prepared")
        return build_album_details_payload(server, provider_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def get_artist_details(server: Any, artist_id: str):
    try:
        return build_artist_details_payload(server, artist_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
