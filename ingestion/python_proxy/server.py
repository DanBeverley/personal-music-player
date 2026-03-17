from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
import requests
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import os
import time
import json
from ytmusicapi import YTMusic
import re

def extract_thumbnail(data):
    if not data: return None
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

class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    seed_id: str = None

class DownloadRequest(BaseModel):
    video_id: str
    title: str = ""

def get_stream_info(video_id: str):
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'no_warnings': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}",
            download=False
        )

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

@app.get("/")
def health_check():
    return {"status": "Auralis Python Proxy is running"}

@app.post("/track_details")
def get_track_details(req: DownloadRequest):
    try:
        video_id = req.video_id
        
        release_date = ""
        artist = ""
        album = ""
        
        try:
            song = ytmusic.get_song(video_id)
            if "microformat" in song and "microformatDataRenderer" in song["microformat"]:
                release_date = song["microformat"]["microformatDataRenderer"].get("publishDate", "")
            vd = song.get("videoDetails", {})
            artist = vd.get("author", "")
        except Exception:
            pass
            
        watch = ytmusic.get_watch_playlist(videoId=video_id)
        
        similar_tracks = []
        for track in watch.get("tracks", []):
            if track.get("videoId") == video_id or not track.get("videoId"):
                continue
            similar_tracks.append({
                "id": track["videoId"],
                "title": track.get("title"),
                "duration": track.get("length") or track.get("duration_seconds") or 0,
                "thumbnail": extract_thumbnail(track),
                "channel": extract_artist(track)
            })

        return {
            "status": "success",
            "video_id": video_id,
            "title": watch.get("videoDetails", {}).get("title"),
            "author": artist,
            "thumbnail": extract_thumbnail(watch.get("videoDetails", {})),
            "duration": watch.get("videoDetails", {}).get("lengthSeconds"),
            "release_date": release_date,
            "album": album, # ytmusic.get_song doesn't easily provide album, leaving blank for now
            "similar_tracks": similar_tracks
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
                return {"status": "success", "results": results}
            except Exception:
                pass # Fallback to normal search if extraction fails

        raw_results = ytmusic.search(query, filter="songs", limit=req.limit)
        for entry in raw_results:
            results.append({
                "id": entry.get("videoId"),
                "title": entry.get("title"),
                "duration": entry.get("duration_seconds") or 0,
                "thumbnail": extract_thumbnail(entry),
                "channel": extract_artist(entry)
            })
        return {"status": "success", "results": results}
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

        return {"status": "success", "recommendations": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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

@app.post("/track_details")
def get_track_details(req: DownloadRequest):
    try:
        watch = ytmusic.get_watch_playlist(videoId=req.video_id)
        if not watch or "tracks" not in watch or len(watch["tracks"]) == 0:
            return {"artist": "Unknown", "release_date": "", "similar_tracks": []}
            
        main_track = watch["tracks"][0]
        artist = ", ".join([a.get("name", "") for a in main_track.get("artists", [])]) if main_track.get("artists") else "Unknown"
        
        similar_tracks = []
        for t in watch["tracks"][1:20]: # limit to 20 similar tracks
            if "videoId" in t and t["videoId"]:
                similar_tracks.append({
                    "id": t["videoId"],
                    "title": t.get("title", ""),
                    "artist": ", ".join([a.get("name", "") for a in t.get("artists", [])]) if t.get("artists") else "",
                    "thumbnail": (t.get("thumbnails") or t.get("thumbnail") or [{}])[-1].get("url") if (t.get("thumbnails") or t.get("thumbnail")) else None
                })
                
        return {
            "artist": artist,
            "release_date": "", 
            "similar_tracks": similar_tracks
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
    port = int(os.environ.get("PORT", 8010))
    print(f"Starting Auralis Proxy Server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
