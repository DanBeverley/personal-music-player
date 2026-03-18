from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import List

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
import json
import os
import re
import time

import requests
import yt_dlp
from ytmusicapi import YTMusic

def extract_thumbnail(data):
    if not data: return None
    video_id = data.get("videoId") or data.get("video_id") or data.get("id")
    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    thumbs = data.get("thumbnails") or data.get("thumbnail")
    if isinstance(thumbs, list) and len(thumbs) > 0:
        return thumbs[-1].get("url")
    elif isinstance(thumbs, dict):
        return thumbs.get("url")
    return None

def extract_artist(data):
    if not data: return "Unknown Artist"
    artists = data.get("artists") or []
    if artists and isinstance(artists, list):
        return ", ".join([a.get("name", "") for a in artists])
    author = data.get("author")
    if author:
        return author.get("name") if isinstance(author, dict) else str(author)
    uploader = data.get("uploader")
    return uploader if uploader else "Unknown Artist"

def extract_album_info(data):
    if not data:
        return None

    album = data.get("album")
    if isinstance(album, dict):
        title = album.get("name") or album.get("title")
        album_id = album.get("id") or album.get("browseId")
        if title:
            return {"id": album_id, "title": title}
    elif isinstance(album, str) and album.strip():
        return {"id": None, "title": album.strip()}

    albums = data.get("albums")
    if isinstance(albums, list) and albums:
        first = albums[0]
        if isinstance(first, dict):
            title = first.get("name") or first.get("title")
            album_id = first.get("id") or first.get("browseId")
            if title:
                return {"id": album_id, "title": title}
        elif isinstance(first, str) and first.strip():
            return {"id": None, "title": first.strip()}
    return None

def normalize_album_results(raw_results):
    albums = []
    seen = set()
    for entry in raw_results or []:
        result_type = (entry.get("resultType") or entry.get("type") or "").lower()
        browse_id = entry.get("browseId") or entry.get("id")
        if result_type and result_type != "album":
            continue
        if not browse_id or browse_id in seen:
            continue
        seen.add(browse_id)
        albums.append({
            "id": browse_id,
            "title": entry.get("title"),
            "artist": extract_artist(entry),
            "thumbnail": extract_thumbnail(entry),
            "year": entry.get("year") or "",
            "track_count": entry.get("trackCount") or entry.get("track_count") or 0,
        })
    return albums

def lookup_album_for_song(video_id: str, title: str, artist: str):
    candidates = []
    try:
        raw_results = ytmusic.search(f"{title} {artist}".strip(), filter="songs", limit=6)
    except Exception:
        raw_results = []

    for entry in raw_results:
        album_info = extract_album_info(entry)
        if not album_info or not album_info.get("title"):
            continue
        score = 0
        if entry.get("videoId") == video_id:
            score += 4
        if title and entry.get("title", "").strip().lower() == title.strip().lower():
            score += 2
        entry_artist = extract_artist(entry)
        if artist and entry_artist and artist.strip().lower() in entry_artist.strip().lower():
            score += 2
        if album_info.get("id"):
            score += 1
        candidates.append((score, album_info))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]

def parse_duration_seconds(value):
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        parts = [int(part) for part in text.split(":") if part.isdigit()]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0

app = FastAPI(title="Auralis Proxy & Recommendation Engine")

# Allow Flutter emulator to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
ytmusic = YTMusic()
STREAM_INFO_TTL_SECONDS = 21600
stream_info_cache = {}
stream_info_inflight = {}
stream_info_lock = Lock()
stream_warm_executor = ThreadPoolExecutor(max_workers=10)

class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    seed_id: str = None

class DownloadRequest(BaseModel):
    video_id: str
    title: str = ""

class WarmStreamRequest(BaseModel):
    video_ids: List[str] = Field(default_factory=list)

def _extract_stream_info(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'no_warnings': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    headers = {}
    for key, value in (info.get("http_headers") or {}).items():
        if key and value:
            headers[str(key)] = str(value)

    return {
        "url": info["url"],
        "headers": headers,
        "mime_type": info.get("ext") or info.get("acodec") or "audio/mp4",
        "duration": info.get("duration") or 0,
    }

def get_stream_info(video_id: str):
    now = time.time()
    with stream_info_lock:
        cached = stream_info_cache.get(video_id)
        if cached and cached["expires_at"] > now:
            return cached["payload"]

        pending = stream_info_inflight.get(video_id)
        if pending is None:
            pending = Future()
            stream_info_inflight[video_id] = pending
            should_extract = True
        else:
            should_extract = False

    if not should_extract:
        return pending.result(timeout=25)

    try:
        payload = _extract_stream_info(video_id)
        with stream_info_lock:
            stream_info_cache[video_id] = {
                "payload": payload,
                "expires_at": now + STREAM_INFO_TTL_SECONDS,
            }
        pending.set_result(payload)
        return payload
    except Exception as exc:
        pending.set_exception(exc)
        raise
    finally:
        with stream_info_lock:
            if stream_info_inflight.get(video_id) is pending:
                stream_info_inflight.pop(video_id, None)

def _warm_stream_safely(video_id: str):
    try:
        get_stream_info(video_id)
    except Exception:
        return

def queue_stream_warmup(video_ids: List[str], limit: int = 18):
    seen = set()
    for video_id in video_ids:
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        stream_warm_executor.submit(_warm_stream_safely, video_id)
        if len(seen) >= limit:
            break

@app.get("/")
def health_check():
    return {"status": "Auralis Python Proxy is running"}

@app.post("/track_details")
def get_track_details(req: DownloadRequest):
    try:
        video_id = req.video_id
        
        release_date = ""
        artist = ""
        album_title = ""
        album_id = None
        
        try:
            song = ytmusic.get_song(video_id)
            if "microformat" in song and "microformatDataRenderer" in song["microformat"]:
                release_date = song["microformat"]["microformatDataRenderer"].get("publishDate", "")
            vd = song.get("videoDetails", {})
            artist = vd.get("author", "")
            song_album = extract_album_info(song) or extract_album_info(vd)
            if song_album:
                album_title = song_album.get("title") or ""
                album_id = song_album.get("id")
        except Exception:
            pass
            
        watch = ytmusic.get_watch_playlist(videoId=video_id)
        video_details = watch.get("videoDetails", {})
        track_title = video_details.get("title") or ""
        if not artist:
            artist = extract_artist(video_details)
        if not album_title:
            looked_up_album = lookup_album_for_song(video_id, track_title, artist)
            if looked_up_album:
                album_title = looked_up_album.get("title") or ""
                album_id = looked_up_album.get("id")
        
        similar_tracks = []
        for track in watch.get("tracks", []):
            if track.get("videoId") == video_id or not track.get("videoId"):
                continue
            similar_tracks.append({
                "id": track["videoId"],
                "title": track.get("title"),
                "duration": track.get("length") or track.get("duration_seconds") or 0,
                "thumbnail": extract_thumbnail(track),
                "channel": extract_artist(track),
                "album": (extract_album_info(track) or {}).get("title"),
                "album_id": (extract_album_info(track) or {}).get("id"),
            })
        queue_stream_warmup([video_id, *[track["id"] for track in similar_tracks]])

        return {
            "status": "success",
            "video_id": video_id,
            "title": track_title,
            "author": artist,
            "thumbnail": extract_thumbnail(video_details),
            "duration": video_details.get("lengthSeconds"),
            "release_date": release_date,
            "album": album_title,
            "album_title": album_title,
            "album_id": album_id,
            "lyrics_available": bool(watch.get("lyrics")),
            "similar_tracks": similar_tracks
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/lyrics/{video_id}")
def get_track_lyrics(video_id: str):
    try:
        watch = ytmusic.get_watch_playlist(videoId=video_id)
        lyrics_browse_id = watch.get("lyrics")
        if not lyrics_browse_id:
            return {
                "status": "success",
                "video_id": video_id,
                "has_lyrics": False,
                "has_timestamps": False,
                "source": None,
                "lines": [],
            }

        lyrics_payload = ytmusic.get_lyrics(lyrics_browse_id, timestamps=True)
        if not lyrics_payload:
            return {
                "status": "success",
                "video_id": video_id,
                "has_lyrics": False,
                "has_timestamps": False,
                "source": None,
                "lines": [],
            }

        lines = []
        if lyrics_payload.get("hasTimestamps"):
            for index, line in enumerate(lyrics_payload.get("lyrics", [])):
                text = getattr(line, "text", "").strip()
                if not text:
                    continue
                lines.append({
                    "index": index,
                    "text": text,
                    "start_time_ms": getattr(line, "start_time", None),
                    "end_time_ms": getattr(line, "end_time", None),
                })
        else:
            raw_text = (lyrics_payload.get("lyrics") or "").splitlines()
            for index, line in enumerate(raw_text):
                text = line.strip()
                if not text:
                    continue
                lines.append({
                    "index": index,
                    "text": text,
                    "start_time_ms": None,
                    "end_time_ms": None,
                })

        return {
            "status": "success",
            "video_id": video_id,
            "has_lyrics": bool(lines),
            "has_timestamps": bool(lyrics_payload.get("hasTimestamps")),
            "source": lyrics_payload.get("source"),
            "lines": lines,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/search")
def search_youtube(req: SearchRequest):
    try:
        query = req.query
        results = []
        
        # Check if the query is a direct YouTube URL
        url_match = re.search(r"(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})", query)
        if url_match:
            video_id = url_match.group(1)
            try:
                watch = ytmusic.get_watch_playlist(videoId=video_id)
                vd = watch.get("videoDetails", {})
                results.append({
                    "id": video_id,
                    "title": vd.get("title") or "Unknown URL Track",
                    "duration": vd.get("lengthSeconds") or 0,
                    "thumbnail": extract_thumbnail(vd),
                    "channel": extract_artist(vd)
                })
                queue_stream_warmup([video_id], limit=1)
                return {"status": "success", "results": results}
            except Exception:
                pass # Fallback to normal search if extraction fails

        raw_results = ytmusic.search(query, filter="songs", limit=req.limit)
        for entry in raw_results:
            album_info = extract_album_info(entry) or {}
            results.append({
                "id": entry.get("videoId"),
                "title": entry.get("title"),
                "duration": entry.get("duration_seconds") or 0,
                "thumbnail": extract_thumbnail(entry),
                "channel": extract_artist(entry),
                "album": album_info.get("title"),
                "album_id": album_info.get("id"),
            })
        queue_stream_warmup([entry.get("id") or entry.get("videoId") for entry in results])
        return {"status": "success", "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/search_albums")
def search_albums(req: SearchRequest):
    try:
        raw_results = ytmusic.search(req.query, filter="albums", limit=req.limit)
        albums = normalize_album_results(raw_results)
        if not albums:
            fallback_results = ytmusic.search(req.query, limit=max(req.limit * 3, 12))
            albums = normalize_album_results(fallback_results)
        return {"status": "success", "albums": albums}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/album/{album_id}")
def get_album_details(album_id: str):
    try:
        album = ytmusic.get_album(album_id)
        album_thumbnail = extract_thumbnail(album)
        album_artist = extract_artist(album)
        tracks = []

        for entry in album.get("tracks", []):
            video_id = entry.get("videoId")
            if not video_id:
                continue
            tracks.append({
                "id": video_id,
                "title": entry.get("title"),
                "duration": parse_duration_seconds(
                    entry.get("duration_seconds")
                    or entry.get("duration")
                    or entry.get("length")
                ),
                "thumbnail": extract_thumbnail(entry) or album_thumbnail,
                "channel": extract_artist(entry) or album_artist,
                "album": album.get("title"),
                "album_title": album.get("title"),
                "album_id": album_id,
            })

        queue_stream_warmup([track["id"] for track in tracks], limit=8)
        return {
            "status": "success",
            "id": album_id,
            "title": album.get("title"),
            "artist": album_artist,
            "thumbnail": album_thumbnail,
            "year": album.get("year") or "",
            "track_count": len(tracks),
            "tracks": tracks,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/suggest")
def get_suggestions(req: SearchRequest):
    try:
        suggestions = ytmusic.get_search_suggestions(req.query)
        # ytmusicapi returns a list of dictionaries like {"text": "querystring"} or just strings
        results = [s.get("text", s) if isinstance(s, dict) else s for s in suggestions]
        return {"status": "success", "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/recommend")
def get_recommendations(req: SearchRequest):
    """
    Fetches genuine YouTube Music recommendations.
    If seed_id is provided (based on user's recent listening history), 
    it generates an infinite mix of similar tracks!
    """
    try:
        results = []
        if req.seed_id and req.seed_id != "trending hit songs":
            # Generate a radio mix based on the seed
            mix = ytmusic.get_watch_playlist(videoId=req.seed_id, limit=req.limit + 5)
            for track in mix.get("tracks", []):
                # Skip the seed track itself
                if track.get("videoId") == req.seed_id or not track.get("videoId"):
                    continue
                results.append({
                    "id": track["videoId"],
                    "title": track.get("title"),
                    "duration": track.get("length") or track.get("duration_seconds") or 0,
                    "thumbnail": extract_thumbnail(track),
                    "channel": extract_artist(track)
                })
                if len(results) >= req.limit:
                    break
            queue_stream_warmup([track.get("id") for track in results])
            return {"status": "success", "recommendations": results}
            
        try:
            home_feed = ytmusic.get_home(limit=req.limit)
        except Exception:
            home_feed = []
            
        results = []
        # Flatten the home feed carousels into a single track list
        for carousel in home_feed:
            for item in carousel.get("contents", []):
                if "videoId" in item and item["videoId"]:
                    results.append({
                        "id": item["videoId"],
                        "title": item.get("title"),
                        "duration": 0, # Home feed doesn't always show seconds, we default to 0
                        "thumbnail": extract_thumbnail(item),
                        "channel": extract_artist(item)
                    })
                # Cap the home screen items to keep it clean natively 
                if len(results) >= req.limit + 10:
                    break
            if len(results) >= req.limit + 10:
                break

        # Fallback if the home feed doesn't have enough direct songs (sometimes it's just playlists)
        if len(results) < 5:
            backup = ytmusic.search("trending hit songs", filter="songs", limit=req.limit)
            for entry in backup:
                results.append({
                    "id": entry.get("videoId"),
                    "title": entry.get("title"),
                    "duration": entry.get("duration_seconds") or 0,
                    "thumbnail": (entry.get("thumbnails") or entry.get("thumbnail") or [{}])[-1].get("url"),
                    "channel": ", ".join([a.get("name", "") for a in entry.get("artists", [])])
                })

        queue_stream_warmup([track.get("id") for track in results])
        return {"status": "success", "recommendations": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/warm_streams")
def warm_streams(req: WarmStreamRequest):
    warmed = {}
    video_ids = [video_id for video_id in req.video_ids[:18] if video_id]
    if not video_ids:
        return {"status": "success", "streams": warmed}

    max_workers = min(10, len(video_ids))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(get_stream_info, video_id): video_id
            for video_id in video_ids
        }
        for future, video_id in future_map.items():
            try:
                warmed[video_id] = future.result(timeout=25)
            except Exception:
                continue
    return {"status": "success", "streams": warmed}

@app.post("/download")
def download_audio(req: DownloadRequest):
    out_path = os.path.join(DOWNLOAD_DIR, f"{req.video_id}.mp3")
    json_cache = os.path.join(DOWNLOAD_DIR, f"{req.video_id}.json")
    
    # Aggressive Cache Purging if Windows Host yt_dlp choked and left a 0-byte MP3 artifact
    if os.path.exists(out_path) and os.path.getsize(out_path) < 100:
        os.remove(out_path)
        if os.path.exists(json_cache):
            os.remove(json_cache)
            
    if os.path.exists(out_path) and os.path.exists(json_cache):
        try:
            with open(json_cache, "r") as f:
                meta = json.load(f)
                meta["message"] = "Already downloaded"
                return meta
        except Exception:
            pass
            
    if os.path.exists(out_path):
        ydl_opts = {'quiet': True, 'no_warnings': True}
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
                    "message": "Already downloaded"
                }
                with open(json_cache, "w") as f:
                    json.dump(meta, f)
                return meta
            except Exception as e:
                # If extraction fails on an existing file, the file must be deleted to allow re-downloads!
                os.remove(out_path)
                raise HTTPException(status_code=500, detail=str(e))

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(DOWNLOAD_DIR, f"{req.video_id}.%(ext)s"),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': False,
        'no_warnings': True,
    }

    url = f"https://www.youtube.com/watch?v={req.video_id}"
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            # We first extract info to get the metadata for the UI (thumbnail, title, byte size)
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
        except Exception as e:
            if "unavailable" in str(e).lower() or "sign in" in str(e).lower():
                search_query = f"ytsearch1:{req.title} audio" if req.title else f"ytsearch1:{req.video_id} audio"
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
                    raise HTTPException(status_code=500, detail=f"Fallback search failed for {req.video_id}")
            else:
                raise HTTPException(status_code=500, detail=str(e))
                
        try:
            with open(os.path.join(DOWNLOAD_DIR, f"{req.video_id}.json"), "w") as f:
                json.dump(meta, f)
        except Exception as e:
            print(f"JSON DUMP ERROR: {e}")
            pass
        return meta

@app.get("/stream/{video_id}")
def stream_audio(video_id: str):
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found. Please download first.")
    
    return FileResponse(file_path, media_type="audio/mpeg", filename=f"{video_id}.mp3")


@app.get("/proxy_stream/{video_id}")
def proxy_stream(video_id: str, request: Request):
    try:
        stream_info = get_stream_info(video_id)
        headers = dict(stream_info["headers"])
        if "range" in request.headers:
            headers["range"] = request.headers["range"]
        req = requests.get(stream_info["url"], headers=headers, stream=True, timeout=30)
        resp_headers = {}
        for k, v in req.headers.items():
            if k.lower() in ['content-type', 'content-length', 'content-range', 'accept-ranges']:
                resp_headers[k] = v
        return StreamingResponse(
            req.iter_content(chunk_size=1024*64), 
            status_code=req.status_code, 
            headers=resp_headers,
            media_type=req.headers.get("content-type", "audio/mp4")
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/direct_url/{video_id}")
def direct_stream_url(video_id: str):
    try:
        stream_info = get_stream_info(video_id)
        return {
            "status": "success",
            "url": stream_info["url"],
            "headers": stream_info["headers"],
            "mime_type": stream_info["mime_type"],
            "duration": stream_info["duration"],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting Auralis Proxy Server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
