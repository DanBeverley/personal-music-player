from __future__ import annotations

import json
import os
from typing import Any

import yt_dlp
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from .stream_core_runtime import (
    classify_stream_failure,
    get_cached_stream_chunk,
    get_stream_info,
    iter_upstream_stream,
    parse_byte_range,
    prepare_streams_with_failures,
)

def warm_streams(server: Any, req: Any):
    prepared, failed = prepare_streams_with_failures(
        server,
        req.video_ids,
        limit=18,
        current_video_id=req.current_video_id,
        active_queue=req.active_queue,
    )
    return {
        "status": "success",
        "streams": prepared,
        "failed": failed,
        "failed_ids": list(failed.keys()),
    }


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
        ydl_opts = {"quiet": True, "no_warnings": True}
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
        "format": "bestaudio/best",
        "outtmpl": os.path.join(server.DOWNLOAD_DIR, f"{req.video_id}.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "quiet": False,
        "no_warnings": True,
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
            if "unavailable" in str(exc).lower() or "sign in" in str(exc).lower():
                search_query = (
                    f"ytsearch1:{req.title} audio"
                    if req.title else f"ytsearch1:{req.video_id} audio"
                )
                info_list = ydl.extract_info(search_query, download=True)
                if "entries" in info_list and len(info_list["entries"]) > 0:
                    info = info_list["entries"][0]
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
                else:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Fallback search failed for {req.video_id}",
                    )
            else:
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
        stream_info = get_stream_info(server, video_id)
        cached_chunk = get_cached_stream_chunk(server, video_id)
        range_header = request.headers.get("range")

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

                    return StreamingResponse(
                        generate_cached_then_upstream(),
                        status_code=status_code,
                        headers=resp_headers,
                        media_type=content_type,
                    )

        total_length = cached_chunk.get("total_length") if cached_chunk else None
        parsed_range = parse_byte_range(range_header, total_length)
        start = parsed_range[0] if parsed_range else 0
        end = parsed_range[1] if parsed_range else None

        def generate_upstream():
            yield from iter_upstream_stream(
                server,
                video_id,
                stream_info,
                start=start,
                end=end,
            )

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
