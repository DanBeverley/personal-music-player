from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from typing import Any

import yt_dlp
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from .stream_cache import (
    cached_response_headers,
    get_stream_cache,
    iter_and_store_full_stream,
    should_cache_full_response,
)
from .stream_core_runtime import (
    _ytdlp_opts,
    classify_stream_failure,
    get_cached_stream_chunk,
    get_stream_info,
    iter_upstream_stream,
    parse_byte_range,
    prepare_streams_with_failures,
)

def _catalog_entity_key(track_key: str) -> str:
    value = str(track_key or "").strip()
    if value.startswith("recording:"):
        recording_id = value.removeprefix("recording:")
        if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", recording_id):
            return f"musicbrainz:recording:{recording_id}"
        return recording_id
    return value


def playback_source_candidates_batch(
    server: Any,
    track_keys: list[str],
) -> dict[str, list[dict[str, str]]]:
    normalized_keys = list(
        dict.fromkeys(str(key or "").strip() for key in track_keys)
    )
    output: dict[str, list[dict[str, str]]] = {
        key: [] for key in normalized_keys if key
    }
    catalog_key_to_tracks: dict[str, list[str]] = {}
    for value in output:
        if value.startswith("audius:"):
            output[value] = [{
                "provider": "audius",
                "source_id": value.removeprefix("audius:"),
                "authority": "trusted_match",
            }]
            continue
        if not value.startswith("recording:") and not value.startswith(
            "musicbrainz:recording:"
        ):
            if (
                re.fullmatch(r"[A-Za-z0-9_-]{11}", value)
                and value.casefold() != "musicbrainz"
            ):
                output[value] = [
                    {
                        "provider": "youtube",
                        "source_id": value,
                        "authority": "legacy",
                    }
                ]
            continue
        catalog_key_to_tracks.setdefault(_catalog_entity_key(value), []).append(value)

    if not catalog_key_to_tracks:
        return output

    from ..recommend.store_runtime import open_recommendation_store_connection

    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return output
    try:
        entity_keys = list(catalog_key_to_tracks)
        placeholders = ",".join("?" for _ in entity_keys)
        rows = connection.execute(
            f"""
            SELECT entity_key, source_provider, source_key, source_authority,
                   confidence,
                   payload_json, updated_at
            FROM catalog_entity_sources
            WHERE entity_type = 'track' AND entity_key IN ({placeholders})
            ORDER BY
                CASE source_authority
                    WHEN 'official' THEN 7
                    WHEN 'official_artist_channel' THEN 6
                    WHEN 'topic' THEN 5
                    WHEN 'label' THEN 4
                    WHEN 'verified_catalog' THEN 3
                    WHEN 'trusted_match' THEN 2
                    ELSE 1
                END DESC,
                confidence DESC,
                updated_at DESC
            """,
            entity_keys,
        ).fetchall()
    except Exception:
        return output
    finally:
        connection.close()
    seen: dict[str, set[tuple[str, str]]] = {key: set() for key in output}
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except Exception:
            payload = {}
        state = str(payload.get("verification_state") or "")
        if state != "verified":
            continue
        provider = str(row["source_provider"] or "").strip().lower()
        source_id = str(row["source_key"] or "").strip()
        if provider == "youtube" and not re.fullmatch(r"[A-Za-z0-9_-]{11}", source_id):
            continue
        if provider == "audius" and not source_id:
            continue
        if provider not in {"youtube", "audius"}:
            continue
        identity = (provider, source_id)
        entity_key = str(row["entity_key"] or "")
        for track_key in catalog_key_to_tracks.get(entity_key, []):
            if identity in seen[track_key]:
                continue
            seen[track_key].add(identity)
            output[track_key].append({
                "provider": provider,
                "source_id": source_id,
                "authority": str(row["source_authority"] or ""),
                "verification_state": state,
                "verified_at": str(payload.get("verified_at") or ""),
            })
    for sources in output.values():
        sources.sort(
            key=lambda item: (
                item.get("verification_state") == "verified",
                item.get("authority")
                in {"official", "official_artist_channel", "topic", "label"},
            ),
            reverse=True,
        )
    return output


def playback_source_candidates(server: Any, track_key: str) -> list[dict[str, str]]:
    value = str(track_key or "").strip()
    return playback_source_candidates_batch(server, [value]).get(value, [])


def _playback_path(track_key: str) -> str:
    return f"/playback/stream/{urllib.parse.quote(str(track_key or ''), safe='')}"


def _audius_stream_url(source_id: str) -> str:
    app_name = urllib.parse.quote(os.environ.get("AUDIUS_APP_NAME", "Neatie"), safe="")
    return f"https://api.audius.co/v1/tracks/{source_id}/stream?app_name={app_name}"


def prepare_playback_tracks(
    server: Any,
    track_keys: list[str],
    *,
    limit: int,
    current_track_key: str | None = None,
    active_queue: bool = False,
    defer_all_chunks: bool = False,
):
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in track_keys:
        key = str(raw or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
        if len(ordered) >= max(int(limit or 1), 1):
            break
    if current_track_key and current_track_key in ordered:
        ordered.remove(current_track_key)
        ordered.insert(0, current_track_key)

    selected = playback_source_candidates_batch(server, ordered)
    youtube_first = {
        key: sources[0]["source_id"]
        for key, sources in selected.items()
        if sources and sources[0]["provider"] == "youtube"
    }
    if youtube_first:
        youtube_prepared, youtube_failed = prepare_streams_with_failures(
            server,
            list(youtube_first.values()),
            limit=len(youtube_first) or 1,
            current_video_id=youtube_first.get(current_track_key or ""),
            active_queue=active_queue,
            defer_all_chunks=defer_all_chunks,
        )
    else:
        youtube_prepared, youtube_failed = {}, {}

    prepared: dict[str, dict[str, Any]] = {}
    failed: dict[str, dict[str, Any]] = {}
    for track_key in ordered:
        sources = selected.get(track_key) or []
        if not sources:
            failed[track_key] = {
                "code": "playback_source_missing",
                "message": "No verified playback source",
            }
            continue
        resolved = None
        last_failure: dict[str, Any] | None = None
        for index, source in enumerate(sources):
            provider = source["provider"]
            source_id = source["source_id"]
            if provider == "audius":
                resolved = {
                    "prepared": True,
                    "playback_path": _playback_path(track_key),
                    "playback_url": _playback_path(track_key),
                    "source_kind": "audius_proxy",
                    "provider": "audius",
                    "source_id": source_id,
                    "headers": {},
                    "resolve_ms": 0,
                    "chunk_ms": 0,
                    "cached_prefix_bytes": 0,
                    "target_chunk_bytes": 0,
                    "expires_at": time.time() + 900,
                }
                break
            metrics = youtube_prepared.get(source_id) if index == 0 else None
            failure = youtube_failed.get(source_id) if index == 0 else None
            if metrics is None and index > 0:
                retry_prepared, retry_failed = prepare_streams_with_failures(
                    server,
                    [source_id],
                    limit=1,
                    current_video_id=source_id,
                    active_queue=active_queue,
                    defer_all_chunks=defer_all_chunks,
                )
                metrics = retry_prepared.get(source_id)
                failure = retry_failed.get(source_id)
            if metrics is not None:
                resolved = {
                    **dict(metrics),
                    "playback_path": _playback_path(track_key),
                    "playback_url": _playback_path(track_key),
                    "provider": "youtube",
                    "source_id": source_id,
                    "headers": {},
                }
                break
            if isinstance(failure, dict):
                last_failure = dict(failure)
        if resolved is not None:
            prepared[track_key] = resolved
        else:
            failed[track_key] = last_failure or {
                "code": "playback_source_failed",
                "message": "All verified playback sources failed",
            }
    return prepared, failed


def download_audio(server: Any, req: Any):
    out_path = os.path.join(server.DOWNLOAD_DIR, f"{req.video_id}.mp3")
    json_cache = os.path.join(server.DOWNLOAD_DIR, f"{req.video_id}.json")

    if os.path.exists(out_path) and os.path.getsize(out_path) < 100:
        os.remove(out_path)
        if os.path.exists(json_cache):
            os.remove(json_cache)

    if os.path.exists(out_path) and os.path.exists(json_cache):
        try:
            with open(json_cache, "r", encoding="utf-8") as file:
                meta = json.load(file)
                meta["message"] = "Already downloaded"
                return meta
        except Exception:
            pass

    if os.path.exists(out_path):
        ydl_opts = _ytdlp_opts(server)
        url = f"https://www.youtube.com/watch?v={req.video_id}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                meta = {
                    "status": "success",
                    "video_id": req.video_id,
                    "title": info.get("title") or "Unknown Track",
                    "thumbnail": info.get("thumbnail"),
                    "duration": info.get("duration") or 0,
                    "filesize": os.path.getsize(out_path),
                    "filename": f"{req.video_id}.mp3",
                    "author": info.get("channel") or info.get("uploader"),
                    "message": "Already downloaded",
                }
                with open(json_cache, "w", encoding="utf-8") as file:
                    json.dump(meta, file)
                return meta
            except Exception as exc:
                os.remove(out_path)
                raise HTTPException(status_code=500, detail=str(exc))

    ydl_opts = {
        **_ytdlp_opts(server),
        "outtmpl": os.path.join(server.DOWNLOAD_DIR, f"{req.video_id}.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    url = f"https://www.youtube.com/watch?v={req.video_id}"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
            meta = {
                "status": "success",
                "video_id": req.video_id,
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration") or 0,
                "filesize": info.get("filesize_approx") or info.get("filesize") or 0,
                "filename": f"{req.video_id}.mp3",
                "author": info.get("channel") or info.get("uploader"),
            }
        except Exception as exc:
            # Downloads use the already-selected playback identity. Searching by
            # title here could silently replace it with an unrelated upload.
            raise HTTPException(status_code=500, detail=str(exc))

        try:
            with open(
                os.path.join(server.DOWNLOAD_DIR, f"{req.video_id}.json"),
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(meta, file)
        except Exception as exc:
            print(f"JSON DUMP ERROR: {exc}")
        return meta


def stream_audio(server: Any, video_id: str):
    file_path = os.path.join(server.DOWNLOAD_DIR, f"{video_id}.mp3")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found. Please download first.")
    return FileResponse(file_path, media_type="audio/mpeg", filename=f"{video_id}.mp3")


def proxy_stream(server: Any, video_id: str, request: Request):
    try:
        range_header = request.headers.get("range")
        stream_cache = get_stream_cache(server)
        stream_cache_meta = stream_cache.head(video_id)
        if stream_cache_meta:
            total_length = int(stream_cache_meta.get("content_length") or 0) or None
            parsed_range = parse_byte_range(range_header, total_length)
            if range_header and parsed_range is None:
                raise HTTPException(
                    status_code=416,
                    detail={"code": "invalid_range", "video_id": video_id},
                )
            start = parsed_range[0] if parsed_range else 0
            end = parsed_range[1] if parsed_range else None
            headers, status_code = cached_response_headers(
                stream_cache_meta,
                start=start,
                end=end,
                ranged=bool(range_header),
            )
            print(
                "[EBB:stream-cache] "
                f"video={video_id} event=hit backend={stream_cache.backend_name} "
                f"bytes={total_length or 0} range={range_header or 'full'}"
            )
            return StreamingResponse(
                stream_cache.iter_object(video_id, start=start, end=end),
                status_code=status_code,
                headers=headers,
                media_type=stream_cache_meta.get("content_type") or "audio/mp4",
            )
        if stream_cache.enabled:
            print(
                "[EBB:stream-cache] "
                f"video={video_id} event=miss backend={stream_cache.backend_name} "
                f"range={range_header or 'full'}"
            )

        stream_info = get_stream_info(server, video_id)
        cached_chunk = get_cached_stream_chunk(server, video_id)

        if cached_chunk:
            cached_bytes = cached_chunk.get("bytes") or b""
            content_type = cached_chunk.get("content_type") or stream_info.get("mime_type") or "audio/mp4"
            total_length = cached_chunk.get("total_length")
            parsed_range = parse_byte_range(range_header, total_length)

            if not range_header or parsed_range is not None:
                start = parsed_range[0] if parsed_range else 0
                end = parsed_range[1] if parsed_range else None

                if start < len(cached_bytes):
                    cached_end = len(cached_bytes) - 1
                    requested_end = end if end is not None else cached_end
                    slice_end = min(cached_end, requested_end)
                    cached_slice = cached_bytes[start:slice_end + 1]

                    def generate_cached_then_upstream():
                        if cached_slice:
                            yield cached_slice

                        upstream_start = len(cached_bytes)
                        if upstream_start <= start:
                            upstream_start = start

                        if end is not None and upstream_start > end:
                            return

                        if total_length is not None and upstream_start >= total_length:
                            return

                        yield from iter_upstream_stream(
                            server,
                            video_id,
                            stream_info,
                            start=upstream_start,
                            end=end,
                        )

                    resp_headers = {"Accept-Ranges": "bytes"}
                    status_code = 206 if range_header else 200
                    if total_length is not None:
                        response_end = end if end is not None else total_length - 1
                        if status_code == 206:
                            resp_headers["Content-Range"] = (
                                f"bytes {start}-{response_end}/{total_length}"
                            )
                            resp_headers["Content-Length"] = str(
                                max(response_end - start + 1, 0)
                            )
                        else:
                            resp_headers["Content-Length"] = str(total_length)
                    elif status_code == 206 and end is not None:
                        resp_headers["Content-Length"] = str(max(end - start + 1, 0))

                    response_iter = generate_cached_then_upstream()
                    if should_cache_full_response(start, end, total_length):
                        response_iter = iter_and_store_full_stream(
                            server,
                            video_id,
                            response_iter,
                            content_type=content_type,
                        )

                    return StreamingResponse(
                        response_iter,
                        status_code=status_code,
                        headers=resp_headers,
                        media_type=content_type,
                    )

        total_length = cached_chunk.get("total_length") if cached_chunk else None
        parsed_range = parse_byte_range(range_header, total_length)
        start = parsed_range[0] if parsed_range else 0
        end = parsed_range[1] if parsed_range else None

        def generate_upstream():
            chunks = iter_upstream_stream(
                server,
                video_id,
                stream_info,
                start=start,
                end=end,
            )
            if should_cache_full_response(start, end, total_length):
                yield from iter_and_store_full_stream(
                    server,
                    video_id,
                    chunks,
                    content_type=stream_info.get("mime_type", "audio/mp4"),
                )
                return
            yield from chunks

        resp_headers = {"Accept-Ranges": "bytes"}
        status_code = 206 if range_header and parsed_range is not None else 200
        if total_length is not None:
            response_end = end if end is not None else total_length - 1
            if status_code == 206:
                resp_headers["Content-Range"] = f"bytes {start}-{response_end}/{total_length}"
                resp_headers["Content-Length"] = str(max(response_end - start + 1, 0))
            elif start == 0:
                resp_headers["Content-Length"] = str(total_length)
        return StreamingResponse(
            generate_upstream(),
            status_code=status_code,
            headers=resp_headers,
            media_type=stream_info.get("mime_type", "audio/mp4"),
        )
    except HTTPException:
        raise
    except Exception as exc:
        classified = classify_stream_failure(exc)
        raise HTTPException(
            status_code=classified["status_code"],
            detail={
                "code": classified["code"],
                "message": classified["message"],
                "video_id": video_id,
            },
        )


def direct_stream_url(server: Any, video_id: str):
    try:
        stream_info = get_stream_info(server, video_id)
        return {
            "status": "success",
            "url": stream_info["url"],
            "headers": stream_info["headers"],
            "mime_type": stream_info["mime_type"],
            "duration": stream_info["duration"],
            "expires_at": stream_info.get("expires_at"),
        }
    except HTTPException:
        raise
    except Exception as exc:
        classified = classify_stream_failure(exc)
        raise HTTPException(
            status_code=classified["status_code"],
            detail={
                "code": classified["code"],
                "message": classified["message"],
                "video_id": video_id,
            },
        )


def playback_resolve(server: Any, track_key: str):
    sources = playback_source_candidates(server, track_key)
    if not sources:
        raise HTTPException(
            status_code=404,
            detail={"code": "playback_source_missing", "track_key": track_key},
        )
    failures: list[str] = []
    for source in sources:
        provider = source["provider"]
        source_id = source["source_id"]
        if provider == "audius":
            return {
                "status": "success",
                "track_key": track_key,
                "provider": provider,
                "source_id": source_id,
                "url": _playback_path(track_key),
                "headers": {},
                "source_kind": "audius_proxy",
            }
        try:
            resolved = direct_stream_url(server, source_id)
            return {
                **resolved,
                "track_key": track_key,
                "provider": provider,
                "source_id": source_id,
                "source_kind": "youtube_direct",
            }
        except HTTPException as exc:
            failures.append(str(exc.detail))
    raise HTTPException(
        status_code=502,
        detail={
            "code": "playback_sources_failed",
            "track_key": track_key,
            "failures": failures,
        },
    )


def _proxy_audius(server: Any, source_id: str, request: Request):
    headers = {}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header
    upstream = server.upstream_http.get(
        _audius_stream_url(source_id),
        headers=headers,
        stream=True,
        timeout=(5, 25),
        allow_redirects=True,
    )
    if upstream.status_code not in {200, 206}:
        upstream.close()
        raise HTTPException(
            status_code=502,
            detail={"code": "audius_stream_failed", "source_id": source_id},
        )

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    response_headers = {"Accept-Ranges": "bytes"}
    for header in ("content-length", "content-range"):
        if upstream.headers.get(header):
            response_headers["-".join(part.capitalize() for part in header.split("-"))] = upstream.headers[header]
    return StreamingResponse(
        generate(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type") or "audio/mpeg",
    )


def playback_stream(server: Any, track_key: str, request: Request):
    sources = playback_source_candidates(server, track_key)
    if not sources:
        raise HTTPException(
            status_code=404,
            detail={"code": "playback_source_missing", "track_key": track_key},
        )
    failures: list[str] = []
    background = str(request.query_params.get("background") or "").casefold() in {
        "1",
        "true",
        "yes",
    }
    youtube_blocked = False
    if background:
        from ..discovery.source_registry import youtube_background_resolution_blocked

        youtube_blocked = youtube_background_resolution_blocked(server)
    for source in sources:
        try:
            if source["provider"] == "audius":
                return _proxy_audius(server, source["source_id"], request)
            if background and youtube_blocked:
                failures.append("youtube_temporarily_blocked")
                continue
            return proxy_stream(server, source["source_id"], request)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            if background and detail.get("code") == "source_blocked":
                from ..discovery.source_registry import record_youtube_background_failures

                record_youtube_background_failures(
                    server,
                    {source["source_id"]: detail},
                )
                youtube_blocked = True
            failures.append(str(exc.detail))
            continue
    raise HTTPException(
        status_code=502,
        detail={
            "code": "playback_sources_failed",
            "track_key": track_key,
            "failures": failures,
        },
    )
