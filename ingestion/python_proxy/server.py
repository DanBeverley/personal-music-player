from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
import json
import hashlib
import math
import os
import re
import sqlite3
import time
import traceback
import uuid

import requests
import yt_dlp
from ytmusicapi import YTMusic

try:
    import psycopg
except Exception:
    psycopg = None

try:
    from langgraph_assistant import (
        langgraph_runtime_available,
        run_langgraph_assistant,
    )
except Exception as exc:
    def langgraph_runtime_available() -> bool:
        return False

    def run_langgraph_assistant(req: Any, deps: Dict[str, Any]):
        raise RuntimeError(f"LangGraph assistant import failed: {exc}")

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


def normalize_artist_results(raw_results):
    artists = []
    seen = set()
    for entry in raw_results or []:
        result_type = (entry.get("resultType") or entry.get("type") or "").lower()
        browse_id = entry.get("browseId") or entry.get("id")
        name = (
            entry.get("artist")
            or entry.get("name")
            or entry.get("title")
            or extract_artist(entry)
        )
        if result_type and result_type not in {"artist", "artists"}:
            continue
        if not browse_id or browse_id in seen or not name:
            continue
        seen.add(browse_id)
        artists.append(
            {
                "id": browse_id,
                "name": name,
                "thumbnail": extract_thumbnail(entry),
                "description": (
                    entry.get("subscribers")
                    or entry.get("description")
                    or entry.get("type")
                    or ""
                ),
            }
        )
    return artists

def _normalize_song_result(entry):
    if not entry:
        return None
    video_id = entry.get("videoId") or entry.get("video_id") or entry.get("id")
    if not video_id:
        return None
    album_info = extract_album_info(entry) or {}
    return {
        "id": video_id,
        "title": entry.get("title"),
        "duration": parse_duration_seconds(
            entry.get("duration_seconds")
            or entry.get("lengthSeconds")
            or entry.get("length")
            or entry.get("duration")
        ),
        "thumbnail": extract_thumbnail(entry),
        "channel": extract_artist(entry),
        "album": album_info.get("title"),
        "album_id": album_info.get("id"),
    }

def _ytmusic_song_search(query: str, limit: int):
    results = []
    seen = set()

    def add_entry(entry):
        normalized = _normalize_song_result(entry)
        if not normalized:
            return
        track_id = normalized["id"]
        if not track_id or track_id in seen:
            return
        seen.add(track_id)
        results.append(normalized)

    try:
        raw_results = ytmusic.search(query, filter="songs", limit=limit)
    except Exception:
        raw_results = []

    for entry in raw_results:
        add_entry(entry)
        if len(results) >= limit:
            return results

    if len(results) < limit:
        try:
            fallback_results = ytmusic.search(query, limit=max(limit * 3, 12))
        except Exception:
            fallback_results = []

        for entry in fallback_results:
            result_type = (entry.get("resultType") or entry.get("type") or "").lower()
            if result_type and result_type not in {"song", "video"}:
                continue
            add_entry(entry)
            if len(results) >= limit:
                break

    return results

def _ytdlp_song_search(query: str, limit: int):
    url = f"ytsearch{limit}:{query}"
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'skip_download': True,
    }
    results = []
    seen = set()
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return results

    for entry in info.get("entries", []) or []:
        video_id = entry.get("id") or entry.get("url")
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        results.append({
            "id": video_id,
            "title": entry.get("title"),
            "duration": parse_duration_seconds(entry.get("duration")),
            "thumbnail": extract_thumbnail(entry),
            "channel": extract_artist(entry),
            "album": None,
            "album_id": None,
        })
        if len(results) >= limit:
            break
    return results

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

def normalize_recommendation_track(data):
    if not data:
        return None
    video_id = data.get("videoId") or data.get("video_id") or data.get("id")
    if not video_id:
        return None
    album_info = extract_album_info(data) or {}
    return {
        "id": video_id,
        "title": data.get("title"),
        "duration": parse_duration_seconds(
            data.get("duration_seconds")
            or data.get("lengthSeconds")
            or data.get("length")
            or data.get("duration")
        ),
        "thumbnail": extract_thumbnail(data),
        "channel": extract_artist(data),
        "album": album_info.get("title"),
        "album_id": album_info.get("id"),
    }

def _normalize_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())

def _query_tokens(query: str):
    return [
        token
        for token in re.split(r"[^a-z0-9]+", _normalize_text(query))
        if len(token) >= 3
    ]

def _ollama_headers():
    headers = {"Content-Type": "application/json"}
    if OLLAMA_API_KEY:
        headers["Authorization"] = f"Bearer {OLLAMA_API_KEY}"
    return headers

def _call_ollama_chat(
    messages,
    *,
    schema=None,
    temperature=0.2,
    timeout_seconds=None,
    model_override=None,
):
    payload = {
        "model": (model_override or OLLAMA_MODEL),
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if schema is not None:
        payload["format"] = schema

    response = ollama_http.post(
        f"{OLLAMA_BASE_URL}/chat",
        headers=_ollama_headers(),
        json=payload,
        timeout=(
            OLLAMA_CONNECT_TIMEOUT_SECONDS,
            timeout_seconds or OLLAMA_READ_TIMEOUT_SECONDS,
        ),
    )
    response.raise_for_status()
    data = response.json()
    return ((data.get("message") or {}).get("content") or "").strip()

def _extract_json_from_llm_text(raw: str):
    text = (raw or "").strip()
    if not text:
        raise ValueError("Empty model response")

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    object_start = text.find("{")
    object_end = text.rfind("}")
    if object_start != -1 and object_end != -1 and object_end > object_start:
        candidate = text[object_start : object_end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    array_start = text.find("[")
    array_end = text.rfind("]")
    if array_start != -1 and array_end != -1 and array_end > array_start:
        candidate = text[array_start : array_end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    raise ValueError(f"Model did not return valid JSON: {text[:200]}")

def _call_ollama_structured(
    messages,
    *,
    schema,
    temperature=0.2,
    max_attempts=3,
    timeout_seconds=None,
    model_override=None,
):
    last_error = None
    for attempt in range(max_attempts):
        attempt_messages = list(messages)
        if attempt > 0:
            attempt_messages.append(
                {
                    "role": "system",
                    "content": (
                        "Your previous response was invalid. "
                        "Return only a single valid JSON object matching the requested schema. "
                        "Do not include markdown fences, commentary, or prose outside JSON."
                    ),
                }
            )
        raw = _call_ollama_chat(
            attempt_messages,
            schema=schema if attempt == 0 else None,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            model_override=model_override,
        )
        try:
            parsed = _extract_json_from_llm_text(raw)
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"Structured LLM call expected a JSON object but received {type(parsed).__name__}"
                )
            return parsed
        except Exception as exc:
            last_error = exc
    raise last_error or ValueError("Structured LLM call failed")

def _playlist_lookup_by_name(playlists, target_name):
    normalized_target = _normalize_text(target_name)
    if not normalized_target:
        return None
    exact_match = None
    fuzzy_match = None
    for playlist in playlists:
        name = _normalize_text(playlist.name)
        if not name:
            continue
        if name == normalized_target:
            exact_match = playlist
            break
        if normalized_target in name or name in normalized_target:
            fuzzy_match = fuzzy_match or playlist
    return exact_match or fuzzy_match

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
STREAM_WARM_CHUNK_BYTES = int(os.environ.get("STREAM_WARM_CHUNK_BYTES", "786432"))
STREAM_CHUNK_TTL_SECONDS = 1800
STREAM_WARM_WORKERS = int(os.environ.get("STREAM_WARM_WORKERS", "8"))
PREPARE_SESSION_WORKERS = int(os.environ.get("PREPARE_SESSION_WORKERS", "4"))
PREPARE_SESSION_MAX_LOOKAHEAD = int(os.environ.get("PREPARE_SESSION_MAX_LOOKAHEAD", "18"))
RECOMMENDATION_CACHE_TTL_SECONDS = int(os.environ.get("RECOMMENDATION_CACHE_TTL_SECONDS", "180"))
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/api").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "minimax-m2.7:cloud")
OLLAMA_THINKING_MODEL = os.environ.get("OLLAMA_THINKING_MODEL", OLLAMA_MODEL).strip() or OLLAMA_MODEL
OLLAMA_FAST_MODEL = os.environ.get("OLLAMA_FAST_MODEL", "rnj-1:8b-cloud").strip() or "rnj-1:8b-cloud"
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "").strip()
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", OLLAMA_MODEL).strip()
OLLAMA_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_CONNECT_TIMEOUT_SECONDS", "10"))
OLLAMA_READ_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_READ_TIMEOUT_SECONDS", "90"))
ASSISTANT_EMBED_BACKEND = os.environ.get("ASSISTANT_EMBED_BACKEND", "local").strip().lower()
ASSISTANT_EMBED_DIM = int(os.environ.get("ASSISTANT_EMBED_DIM", "256"))
USE_LANGGRAPH_ASSISTANT = os.environ.get("USE_LANGGRAPH_ASSISTANT", "1").strip().lower() not in {"0", "false", "no"}
ASSISTANT_VECTOR_BACKEND = os.environ.get("ASSISTANT_VECTOR_BACKEND", "sqlite").strip().lower()
ASSISTANT_PGVECTOR_DSN = os.environ.get("ASSISTANT_PGVECTOR_DSN", os.environ.get("DATABASE_URL", "")).strip()
ASSISTANT_MEMORY_DB_PATH = os.path.join(os.getcwd(), "assistant_memory.sqlite")
stream_info_cache = {}
stream_info_inflight = {}
stream_info_lock = Lock()
stream_chunk_cache = {}
stream_chunk_lock = Lock()
stream_chunk_inflight = {}
stream_chunk_inflight_lock = Lock()
prepare_metrics = deque(maxlen=180)
prepare_metrics_lock = Lock()
recommendation_cache = {}
recommendation_cache_lock = Lock()
home_candidates_cache = {"expires_at": 0, "results": []}
home_candidates_lock = Lock()
stream_warm_executor = ThreadPoolExecutor(max_workers=STREAM_WARM_WORKERS)
upstream_http = requests.Session()
ollama_http = requests.Session()
assistant_memory_lock = Lock()

class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    seed_id: str = None
    seed_ids: List[str] = Field(default_factory=list)
    taste_queries: List[str] = Field(default_factory=list)
    artist_hints: List[str] = Field(default_factory=list)
    avoid_ids: List[str] = Field(default_factory=list)
    offset: int = 0

class DownloadRequest(BaseModel):
    video_id: str
    title: str = ""

class WarmStreamRequest(BaseModel):
    video_ids: List[str] = Field(default_factory=list)
    current_video_id: Optional[str] = None
    active_queue: bool = False
    lookahead: int = 0

class AssistantConversationMessage(BaseModel):
    role: str
    content: str

class AssistantPlaylistSummary(BaseModel):
    id: str
    name: str
    track_count: int = 0

class AssistantLibraryTrack(BaseModel):
    id: Optional[str] = None
    title: str = ""
    artist: str = ""
    album: Optional[str] = None

class AssistantContextTrack(BaseModel):
    id: Optional[str] = None
    title: str = ""
    artist: str = ""
    channel: Optional[str] = None
    album: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: int = 0
    reason: Optional[str] = None

class AssistantChatRequest(BaseModel):
    message: str
    user_scope_id: str = "guest"
    session_id: Optional[str] = None
    thinking_mode: bool = True
    conversation: List[AssistantConversationMessage] = Field(default_factory=list)
    last_assistant_tracks: List[AssistantContextTrack] = Field(default_factory=list)
    last_playlist_draft_tracks: List[AssistantContextTrack] = Field(default_factory=list)
    recent_assistant_tracks: List[AssistantContextTrack] = Field(default_factory=list)
    playlist_summaries: List[AssistantPlaylistSummary] = Field(default_factory=list)
    recent_track_ids: List[str] = Field(default_factory=list)
    recent_queries: List[str] = Field(default_factory=list)
    library_tracks: List[AssistantLibraryTrack] = Field(default_factory=list)
    limit: int = 10


class AssistantSessionCreateRequest(BaseModel):
    user_scope_id: str = "guest"
    title: Optional[str] = None


class AssistantSessionUpdateRequest(BaseModel):
    user_scope_id: str = "guest"
    title: Optional[str] = None
    archived: Optional[bool] = None
    pinned: Optional[bool] = None

def _assistant_db_connection():
    connection = sqlite3.connect(ASSISTANT_MEMORY_DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection

def _assistant_pgvector_enabled():
    return (
        ASSISTANT_VECTOR_BACKEND == "pgvector"
        and psycopg is not None
        and bool(ASSISTANT_PGVECTOR_DSN)
    )

def _assistant_pgvector_connection():
    if not _assistant_pgvector_enabled():
        return None
    connection = psycopg.connect(ASSISTANT_PGVECTOR_DSN)
    connection.autocommit = True
    return connection

def _assistant_vector_literal(embedding: Optional[List[float]]):
    if not embedding:
        return None
    return "[" + ",".join(f"{float(value):.8f}" for value in embedding) + "]"

def _assistant_init_memory_db():
    with assistant_memory_lock:
        if _assistant_pgvector_enabled():
            connection = _assistant_pgvector_connection()
            if connection is not None:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
                        cursor.execute(
                            f"""
                            CREATE TABLE IF NOT EXISTS assistant_memory (
                                id TEXT PRIMARY KEY,
                                scope_id TEXT NOT NULL,
                                kind TEXT NOT NULL,
                                content TEXT NOT NULL,
                                metadata_json JSONB,
                                embedding VECTOR({ASSISTANT_EMBED_DIM}),
                                created_at DOUBLE PRECISION NOT NULL
                            )
                            """
                        )
                        cursor.execute(
                            "CREATE INDEX IF NOT EXISTS idx_assistant_memory_scope_time "
                            "ON assistant_memory(scope_id, created_at DESC)"
                        )
                    return
                except Exception:
                    pass
                finally:
                    connection.close()

        connection = _assistant_db_connection()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_memory (
                    id TEXT PRIMARY KEY,
                    scope_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT,
                    embedding_json TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_assistant_memory_scope_time "
                "ON assistant_memory(scope_id, created_at DESC)"
            )
            connection.commit()
        finally:
            connection.close()

def _assistant_safe_scope_id(scope_id: Optional[str]):
    cleaned = (scope_id or "guest").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "_", cleaned)
    return cleaned or "guest"


def _assistant_now_timestamp():
    return time.time()


def _assistant_default_session_title(message: Optional[str]):
    text = re.sub(r"\s+", " ", (message or "").strip())
    if not text:
        return "New chat"
    return text[:72].rstrip(" .,!?:;") or "New chat"


def _assistant_preview_text(text: Optional[str], limit: int = 180):
    preview = re.sub(r"\s+", " ", (text or "").strip())
    if len(preview) <= limit:
        return preview
    return preview[: limit - 1].rstrip() + "…"


def _assistant_init_session_db():
    connection = _assistant_db_connection()
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS assistant_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_message_preview TEXT,
                last_mode TEXT,
                archived_at REAL,
                pinned_at REAL
            )
            """
        )
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(assistant_sessions)").fetchall()
        }
        if "archived_at" not in existing_columns:
            connection.execute("ALTER TABLE assistant_sessions ADD COLUMN archived_at REAL")
        if "pinned_at" not in existing_columns:
            connection.execute("ALTER TABLE assistant_sessions ADD COLUMN pinned_at REAL")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_assistant_sessions_user_updated "
            "ON assistant_sessions(user_id, updated_at DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS assistant_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                payload_json TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_assistant_messages_session_time "
            "ON assistant_messages(session_id, created_at ASC)"
        )
        connection.commit()
    finally:
        connection.close()


def _assistant_session_summary_from_row(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "title": row["title"] or "New chat",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_message_preview": row["last_message_preview"] or "",
        "last_mode": row["last_mode"] or "",
        "archived_at": row["archived_at"],
        "pinned_at": row["pinned_at"],
    }


def _assistant_list_sessions(user_scope_id: str, include_archived: bool = False):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    connection = _assistant_db_connection()
    try:
        query = (
            "SELECT id, user_id, title, created_at, updated_at, "
            "last_message_preview, last_mode, archived_at, pinned_at "
            "FROM assistant_sessions WHERE user_id = ?"
        )
        params: List[Any] = [scope_id]
        if not include_archived:
            query += " AND archived_at IS NULL"
        query += " ORDER BY pinned_at IS NULL ASC, pinned_at DESC, updated_at DESC"
        rows = connection.execute(query, params).fetchall()
        return [_assistant_session_summary_from_row(row) for row in rows]
    finally:
        connection.close()


def _assistant_get_session(session_id: str, user_scope_id: str):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    connection = _assistant_db_connection()
    try:
        row = connection.execute(
            """
            SELECT id, user_id, title, created_at, updated_at,
                   last_message_preview, last_mode, archived_at, pinned_at
            FROM assistant_sessions
            WHERE id = ? AND user_id = ?
            """,
            [session_id, scope_id],
        ).fetchone()
        return _assistant_session_summary_from_row(row)
    finally:
        connection.close()


def _assistant_create_session(
    user_scope_id: str,
    *,
    title: Optional[str] = None,
    seed_message: Optional[str] = None,
):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    session_id = str(uuid.uuid4())
    now = _assistant_now_timestamp()
    resolved_title = (title or "").strip() or _assistant_default_session_title(seed_message)
    connection = _assistant_db_connection()
    try:
        connection.execute(
            """
            INSERT INTO assistant_sessions (
                id, user_id, title, created_at, updated_at,
                last_message_preview, last_mode, archived_at, pinned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            [
                session_id,
                scope_id,
                resolved_title,
                now,
                now,
                _assistant_preview_text(seed_message),
                "",
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return _assistant_get_session(session_id, scope_id)


def _assistant_touch_session(
    session_id: str,
    user_scope_id: str,
    *,
    title: Optional[str] = None,
    last_message_preview: Optional[str] = None,
    last_mode: Optional[str] = None,
):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    updates = ["updated_at = ?"]
    params: List[Any] = [_assistant_now_timestamp()]
    if title is not None:
        updates.append("title = ?")
        params.append(title.strip() or "New chat")
    if last_message_preview is not None:
        updates.append("last_message_preview = ?")
        params.append(_assistant_preview_text(last_message_preview))
    if last_mode is not None:
        updates.append("last_mode = ?")
        params.append((last_mode or "").strip())
    params.extend([session_id, scope_id])

    connection = _assistant_db_connection()
    try:
        connection.execute(
            f"""
            UPDATE assistant_sessions
            SET {", ".join(updates)}
            WHERE id = ? AND user_id = ?
            """,
            params,
        )
        connection.commit()
    finally:
        connection.close()


def _assistant_store_session_message(
    session_id: str,
    user_scope_id: str,
    *,
    role: str,
    content: str,
    payload: Optional[Dict[str, Any]] = None,
):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    connection = _assistant_db_connection()
    try:
        connection.execute(
            """
            INSERT INTO assistant_messages (
                id, session_id, user_id, role, content, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                str(uuid.uuid4()),
                session_id,
                scope_id,
                role,
                (content or "").strip(),
                json.dumps(payload or {}, ensure_ascii=False),
                _assistant_now_timestamp(),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _assistant_get_session_messages(session_id: str, user_scope_id: str):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    connection = _assistant_db_connection()
    try:
        rows = connection.execute(
            """
            SELECT id, session_id, user_id, role, content, payload_json, created_at
            FROM assistant_messages
            WHERE session_id = ? AND user_id = ?
            ORDER BY created_at ASC
            """,
            [session_id, scope_id],
        ).fetchall()
    finally:
        connection.close()

    messages = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        messages.append(
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "role": row["role"],
                "content": row["content"],
                "payload": payload,
                "created_at": row["created_at"],
            }
        )
    return messages


def _assistant_get_session_detail(session_id: str, user_scope_id: str):
    session = _assistant_get_session(session_id, user_scope_id)
    if session is None:
        return None
    return {
        "session": session,
        "messages": _assistant_get_session_messages(session_id, user_scope_id),
    }


def _assistant_update_session(
    session_id: str,
    user_scope_id: str,
    *,
    title: Optional[str] = None,
    archived: Optional[bool] = None,
    pinned: Optional[bool] = None,
):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    updates = ["updated_at = ?"]
    params: List[Any] = [_assistant_now_timestamp()]
    if title is not None:
        updates.append("title = ?")
        params.append(title.strip() or "New chat")
    if archived is not None:
        updates.append("archived_at = ?")
        params.append(_assistant_now_timestamp() if archived else None)
    if pinned is not None:
        updates.append("pinned_at = ?")
        params.append(_assistant_now_timestamp() if pinned else None)
    params.extend([session_id, scope_id])

    connection = _assistant_db_connection()
    try:
        connection.execute(
            f"""
            UPDATE assistant_sessions
            SET {", ".join(updates)}
            WHERE id = ? AND user_id = ?
            """,
            params,
        )
        connection.commit()
    finally:
        connection.close()
    return _assistant_get_session(session_id, scope_id)


def _assistant_delete_session(session_id: str, user_scope_id: str):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    connection = _assistant_db_connection()
    try:
        connection.execute(
            "DELETE FROM assistant_messages WHERE session_id = ? AND user_id = ?",
            [session_id, scope_id],
        )
        cursor = connection.execute(
            "DELETE FROM assistant_sessions WHERE id = ? AND user_id = ?",
            [session_id, scope_id],
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()

def _assistant_embed_texts(texts: List[str]):
    payload_texts = [text.strip() for text in texts if text and text.strip()]
    if not payload_texts:
        return []

    def local_embed(value: str):
        vector = [0.0] * ASSISTANT_EMBED_DIM
        tokens = _query_tokens(value)
        if not tokens:
            tokens = [token for token in re.split(r"[^a-z0-9]+", _normalize_text(value)) if token]
        if not tokens:
            return vector
        for index, token in enumerate(tokens):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            slot = int.from_bytes(digest[:4], "big") % ASSISTANT_EMBED_DIM
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            weight = 1.15 if index < 6 else 1.0
            vector[slot] += sign * weight
        norm = math.sqrt(sum(item * item for item in vector))
        if norm > 0:
            vector = [item / norm for item in vector]
        return vector

    if ASSISTANT_EMBED_BACKEND == "ollama":
        try:
            response = ollama_http.post(
                f"{OLLAMA_BASE_URL}/embed",
                headers=_ollama_headers(),
                json={
                    "model": OLLAMA_EMBED_MODEL,
                    "input": payload_texts,
                },
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
            embeddings = data.get("embeddings")
            if isinstance(embeddings, list) and embeddings:
                return embeddings
            embedding = data.get("embedding")
            if isinstance(embedding, list) and embedding:
                return [embedding]
        except Exception:
            pass
    return [local_embed(text) for text in payload_texts]

def _assistant_cosine_similarity(a: List[float], b: List[float]):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for left, right in zip(a, b):
        dot += left * right
        norm_a += left * left
        norm_b += right * right
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))

def _assistant_store_memory(scope_id: str, kind: str, content: str, metadata: Optional[Dict[str, Any]] = None):
    text = (content or "").strip()
    if not text:
        return

    embeddings = _assistant_embed_texts([text])
    embedding = embeddings[0] if embeddings else None
    if _assistant_pgvector_enabled():
        connection = _assistant_pgvector_connection()
        if connection is not None:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO assistant_memory(id, scope_id, kind, content, metadata_json, embedding, created_at)
                        VALUES (%s, %s, %s, %s, %s::jsonb, %s::vector, %s)
                        """,
                        (
                            str(uuid.uuid4()),
                            _assistant_safe_scope_id(scope_id),
                            kind,
                            text,
                            json.dumps(metadata or {}, ensure_ascii=False),
                            _assistant_vector_literal(embedding),
                            time.time(),
                        ),
                    )
                return
            except Exception:
                pass
            finally:
                connection.close()

    connection = _assistant_db_connection()
    try:
        connection.execute(
            """
            INSERT INTO assistant_memory(id, scope_id, kind, content, metadata_json, embedding_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                _assistant_safe_scope_id(scope_id),
                kind,
                text,
                json.dumps(metadata or {}, ensure_ascii=False),
                json.dumps(embedding) if embedding is not None else None,
                time.time(),
            ),
        )
        connection.commit()
    finally:
        connection.close()

def _assistant_query_memory(scope_id: str, queries: List[str], limit: int = 6):
    cleaned_queries = [query.strip() for query in queries if query and query.strip()]
    if not cleaned_queries:
        return []

    query_embeddings = _assistant_embed_texts(cleaned_queries[:3])
    if not query_embeddings:
        return []

    if _assistant_pgvector_enabled():
        connection = _assistant_pgvector_connection()
        if connection is not None:
            try:
                rows = []
                with connection.cursor() as cursor:
                    for query_embedding in query_embeddings[:3]:
                        cursor.execute(
                            """
                            SELECT id, kind, content, metadata_json::text, created_at,
                                   (1 - (embedding <=> %s::vector)) AS score
                            FROM assistant_memory
                            WHERE scope_id = %s
                            ORDER BY embedding <=> %s::vector ASC, created_at DESC
                            LIMIT %s
                            """,
                            (
                                _assistant_vector_literal(query_embedding),
                                _assistant_safe_scope_id(scope_id),
                                _assistant_vector_literal(query_embedding),
                                max(limit * 3, 12),
                            ),
                        )
                        rows.extend(cursor.fetchall())
                deduped = {}
                for row in rows:
                    row_id = row[0]
                    current = deduped.get(row_id)
                    candidate = {
                        "id": row[0],
                        "kind": row[1],
                        "content": row[2],
                        "metadata": json.loads(row[3] or "{}"),
                        "score": float(row[5] or 0),
                        "created_at": row[4],
                    }
                    if current is None or candidate["score"] > current["score"]:
                        deduped[row_id] = candidate
                ranked = sorted(
                    deduped.values(),
                    key=lambda item: (item["score"], item["created_at"]),
                    reverse=True,
                )
                return ranked[:limit]
            except Exception:
                pass
            finally:
                connection.close()

    connection = _assistant_db_connection()
    try:
        rows = connection.execute(
            """
            SELECT id, kind, content, metadata_json, embedding_json, created_at
            FROM assistant_memory
            WHERE scope_id = ?
            ORDER BY created_at DESC
            LIMIT 200
            """,
            (_assistant_safe_scope_id(scope_id),),
        ).fetchall()
    finally:
        connection.close()

    scored = []
    for row in rows:
        raw_embedding = row["embedding_json"]
        if not raw_embedding:
            continue
        try:
            embedding = json.loads(raw_embedding)
        except Exception:
            continue
        best_score = 0.0
        for query_embedding in query_embeddings:
            best_score = max(best_score, _assistant_cosine_similarity(query_embedding, embedding))
        scored.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "content": row["content"],
                "metadata": json.loads(row["metadata_json"] or "{}"),
                "score": best_score,
                "created_at": row["created_at"],
            }
        )

    scored.sort(key=lambda item: (item["score"], item["created_at"]), reverse=True)
    return scored[:limit]

def _assistant_track_from_context(track):
    if isinstance(track, BaseModel):
        track = track.model_dump() if hasattr(track, "model_dump") else track.dict()
    raw = dict(track or {})
    track_id = (raw.get("id") or "").strip()
    if not track_id:
        return None
    artist = raw.get("artist") or raw.get("channel") or ""
    return {
        "id": track_id,
        "title": raw.get("title") or "Unknown Track",
        "channel": artist,
        "artist": artist,
        "album": raw.get("album"),
        "thumbnail": raw.get("thumbnail"),
        "duration": parse_duration_seconds(raw.get("duration")),
        "reason": raw.get("reason"),
    }

def _assistant_all_context_tracks(req: AssistantChatRequest):
    groups = {
        "last_assistant_tracks": req.last_assistant_tracks,
        "last_playlist_draft_tracks": req.last_playlist_draft_tracks,
        "recent_assistant_tracks": req.recent_assistant_tracks,
    }
    normalized = {}
    for key, tracks in groups.items():
        normalized[key] = [
            track
            for track in (_assistant_track_from_context(entry) for entry in tracks)
            if track is not None
        ]
    return normalized

def _assistant_tool_search_tracks(query: str, limit: int):
    query = (query or "").strip()
    if not query:
        return []
    results = _ytmusic_song_search(query, limit)
    if len(results) < limit:
        seen = {track["id"] for track in results if track.get("id")}
        for track in _ytdlp_song_search(query, limit - len(results)):
            track_id = track.get("id")
            if not track_id or track_id in seen:
                continue
            seen.add(track_id)
            results.append(track)
            if len(results) >= limit:
                break
    return results[:limit]


def _assistant_tool_search_albums(query: str, limit: int):
    query = (query or "").strip()
    if not query:
        return []
    try:
        raw_results = ytmusic.search(query, filter="albums", limit=limit)
    except Exception:
        raw_results = []
    albums = normalize_album_results(raw_results)
    if not albums:
        try:
            fallback_results = ytmusic.search(query, limit=max(limit * 3, 12))
        except Exception:
            fallback_results = []
        albums = normalize_album_results(fallback_results)
    return albums[:limit]


def _assistant_tool_search_artists(query: str, limit: int):
    query = (query or "").strip()
    if not query:
        return []
    try:
        raw_results = ytmusic.search(query, filter="artists", limit=limit)
    except Exception:
        raw_results = []
    artists = normalize_artist_results(raw_results)
    if not artists:
        try:
            fallback_results = ytmusic.search(query, limit=max(limit * 3, 12))
        except Exception:
            fallback_results = []
        artists = normalize_artist_results(fallback_results)
    return artists[:limit]


def _build_track_details_payload(video_id: str):
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
        "similar_tracks": similar_tracks,
    }


def _build_album_details_payload(album_id: str):
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


def _assistant_tool_get_track_details(video_id: str):
    video_id = (video_id or "").strip()
    if not video_id:
        return {}
    try:
        return _build_track_details_payload(video_id)
    except Exception:
        return {}


def _assistant_tool_get_album_details(album_id: str):
    album_id = (album_id or "").strip()
    if not album_id:
        return {}
    try:
        return _build_album_details_payload(album_id)
    except Exception:
        return {}


def _assistant_tool_get_similar_tracks(video_id: str, limit: int):
    details = _assistant_tool_get_track_details(video_id)
    similar = details.get("similar_tracks")
    if not isinstance(similar, list):
        return []
    return similar[: max(1, min(limit, 12))]


def _assistant_tool_get_user_taste_profile(req: AssistantChatRequest):
    artist_counts = {}
    for entry in list(req.last_assistant_tracks) + list(req.recent_assistant_tracks):
        artist = (entry.artist or "").strip()
        if not artist:
            continue
        artist_counts[artist] = artist_counts.get(artist, 0) + 1

    top_artists = [
        artist
        for artist, _ in sorted(
            artist_counts.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:5]
    ]
    return {
        "recent_queries": list(req.recent_queries[:5]),
        "recent_track_ids": list(req.recent_track_ids[:8]),
        "playlist_names": [playlist.name for playlist in req.playlist_summaries[:6]],
        "library_track_count": len(req.library_tracks),
        "top_recent_artists": top_artists,
    }

def _assistant_tool_use_context_tracks(
    req: AssistantChatRequest,
    source: str,
    count: int,
    artist_filter: Optional[str] = None,
):
    groups = _assistant_all_context_tracks(req)
    selected = groups.get(source) or []
    if not selected and source != "recent_assistant_tracks":
        selected = groups.get("recent_assistant_tracks") or []
    if artist_filter:
        normalized_artist = _normalize_text(artist_filter)
        filtered = [
            track
            for track in selected
            if normalized_artist in _normalize_text(track.get("channel") or track.get("artist"))
        ]
        if filtered:
            selected = filtered
    count = max(1, min(count, 12))
    return selected[:count]

def _assistant_tool_list_playlists(req: AssistantChatRequest):
    return [
        {
            "id": playlist.id,
            "name": playlist.name,
            "track_count": playlist.track_count,
        }
        for playlist in req.playlist_summaries[:20]
    ]

def _assistant_attach_reasons_runtime(tracks, reasons):
    reason_map = {}
    for row in reasons or []:
        if not isinstance(row, dict):
            continue
        track_id = row.get("id")
        reason = (row.get("reason") or "").strip()
        if track_id and reason:
            reason_map[track_id] = reason
    enriched = []
    for track in tracks:
        copy = dict(track)
        track_id = copy.get("id")
        if track_id in reason_map:
            copy["reason"] = reason_map[track_id]
        enriched.append(copy)
    return enriched

def _assistant_store_turn_memory(
    req: AssistantChatRequest,
    response_payload: Dict[str, Any],
    selected_tracks: Optional[List[Dict[str, Any]]] = None,
    target_playlist: Optional[Dict[str, Any]] = None,
):
    scope_id = _assistant_safe_scope_id(req.user_scope_id)
    _assistant_store_memory(
        scope_id,
        "user_message",
        req.message,
        {
            "conversation_length": len(req.conversation),
        },
    )
    reply = (response_payload.get("reply") or "").strip()
    if reply:
        _assistant_store_memory(
            scope_id,
            "assistant_reply",
            reply,
            {
                "action_type": response_payload.get("action_type"),
                "selected_track_ids": response_payload.get("selected_track_ids") or [],
            },
        )
    if selected_tracks:
        summary = "; ".join(
            f"{track.get('title') or 'Unknown Track'} - {track.get('channel') or track.get('artist') or 'Unknown Artist'}"
            for track in selected_tracks[:8]
        )
        _assistant_store_memory(
            scope_id,
            "assistant_tracks",
            summary,
            {
                "track_ids": [
                    track.get("id")
                    for track in selected_tracks
                    if track.get("id")
                ],
                "action_type": response_payload.get("action_type"),
            },
        )
    playlist_name = (response_payload.get("playlist_name") or "").strip()
    if playlist_name:
        _assistant_store_memory(
            scope_id,
            "playlist_intent",
            playlist_name,
            {
                "summary": response_payload.get("playlist_summary"),
                "target_playlist": target_playlist or {},
            },
        )

def _assistant_initial_memory_queries(req: AssistantChatRequest):
    queries = [req.message]
    for entry in req.conversation[-4:]:
        if entry.content and entry.content.strip():
            queries.append(entry.content.strip())
    for query in req.recent_queries[:3]:
        if query and query.strip():
            queries.append(query.strip())
    deduped = []
    seen = set()
    for query in queries:
        normalized = _normalize_text(query)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(query)
    return deduped[:6]

def _assistant_merge_memory_hits(*groups):
    merged = []
    seen = set()
    for group in groups:
        for hit in group or []:
            hit_id = hit.get("id")
            if not hit_id or hit_id in seen:
                continue
            seen.add(hit_id)
            merged.append(hit)
    merged.sort(key=lambda item: (item.get("score", 0.0), item.get("created_at", 0.0)), reverse=True)
    return merged[:8]

def _assistant_playlist_options(req: AssistantChatRequest, preferred_names: Optional[List[str]] = None):
    available = list(req.playlist_summaries[:20])
    if not preferred_names:
        return [
            {
                "id": playlist.id,
                "name": playlist.name,
                "track_count": playlist.track_count,
            }
            for playlist in available[:12]
        ]

    resolved = []
    seen_ids = set()
    for name in preferred_names:
        playlist = _playlist_lookup_by_name(available, name)
        if playlist is None or playlist.id in seen_ids:
            continue
        seen_ids.add(playlist.id)
        resolved.append(
            {
                "id": playlist.id,
                "name": playlist.name,
                "track_count": playlist.track_count,
            }
        )
    return resolved or [
        {
            "id": playlist.id,
            "name": playlist.name,
            "track_count": playlist.track_count,
        }
        for playlist in available[:12]
    ]

def _assistant_model_for_request(req: AssistantChatRequest):
    return OLLAMA_THINKING_MODEL if req.thinking_mode else OLLAMA_FAST_MODEL

def _assistant_langgraph_deps(req: AssistantChatRequest):
    selected_model = _assistant_model_for_request(req)
    return {
        "initial_memory_queries": _assistant_initial_memory_queries,
        "query_memory": _assistant_query_memory,
        "merge_memory_hits": _assistant_merge_memory_hits,
        "call_structured": lambda messages, **kwargs: _call_ollama_structured(
            messages,
            model_override=selected_model,
            **kwargs,
        ),
        "call_chat": lambda messages, **kwargs: _call_ollama_chat(
            messages,
            model_override=selected_model,
            **kwargs,
        ),
        "tool_search_tracks": _assistant_tool_search_tracks,
        "tool_search_albums": _assistant_tool_search_albums,
        "tool_search_artists": _assistant_tool_search_artists,
        "tool_get_track_details": _assistant_tool_get_track_details,
        "tool_get_album_details": _assistant_tool_get_album_details,
        "tool_get_similar_tracks": _assistant_tool_get_similar_tracks,
        "tool_get_user_taste_profile": _assistant_tool_get_user_taste_profile,
        "tool_use_context_tracks": _assistant_tool_use_context_tracks,
        "tool_list_playlists": _assistant_tool_list_playlists,
        "all_context_tracks": _assistant_all_context_tracks,
        "attach_reasons": _assistant_attach_reasons_runtime,
        "playlist_lookup_by_name": _playlist_lookup_by_name,
        "playlist_options": _assistant_playlist_options,
        "store_turn_memory": _assistant_store_turn_memory,
        "fallback_chat_reply": lambda request: _assistant_fallback_chat_reply(
            request,
            model_override=selected_model,
        ),
        "model_name": selected_model,
    }

def _assistant_fallback_chat_reply(req: AssistantChatRequest, model_override=None):
    messages = [
        {
            "role": "system",
            "content": (
                "You are EBB, a warm conversational assistant. "
                "Reply naturally to the user's latest message. "
                "If they want comfort or conversation, be present and human. "
                "Do not force music suggestions unless they explicitly ask for music. "
                "Respond in 2 to 5 sentences."
            ),
        }
    ]
    for entry in req.conversation[-8:]:
        messages.append(
            {
                "role": "assistant" if entry.role == "assistant" else "user",
                "content": entry.content,
            }
        )
    messages.append({"role": "user", "content": req.message})
    reply = _call_ollama_chat(
        messages,
        temperature=0.6,
        model_override=model_override,
    )
    reply = (reply or "").strip()
    if reply:
        return reply
    return "I'm here with you. Tell me a little more and I'll stay with you."

_assistant_init_memory_db()
_assistant_init_session_db()

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

def _is_stream_info_cached(video_id: str):
    now = time.time()
    with stream_info_lock:
        cached = stream_info_cache.get(video_id)
        return bool(cached and cached["expires_at"] > now)

def _extract_total_length(headers):
    content_range = headers.get("content-range") or headers.get("Content-Range")
    if content_range and "/" in content_range:
        total = content_range.split("/")[-1].strip()
        if total.isdigit():
            return int(total)

    content_length = headers.get("content-length") or headers.get("Content-Length")
    if content_length and content_length.isdigit():
        return int(content_length)
    return None

def _get_cached_stream_chunk(video_id: str, min_bytes: int = 0):
    now = time.time()
    with stream_chunk_lock:
        cached = stream_chunk_cache.get(video_id)
        if cached and cached["expires_at"] > now:
            payload = cached["payload"]
            if len(payload.get("bytes") or b"") >= min_bytes:
                return payload
            return None
        if cached:
            stream_chunk_cache.pop(video_id, None)
    return None

def _store_stream_chunk(video_id: str, payload):
    with stream_chunk_lock:
        stream_chunk_cache[video_id] = {
            "payload": payload,
            "expires_at": time.time() + STREAM_CHUNK_TTL_SECONDS,
        }

def _chunk_target_bytes(position: int, active_queue: bool):
    if active_queue:
        if position <= 0:
            return 2097152
        if position == 1:
            return 1572864
        if position == 2:
            return 1048576
        return STREAM_WARM_CHUNK_BYTES
    if position <= 0:
        return 1048576
    if position == 1:
        return 786432
    return STREAM_WARM_CHUNK_BYTES

def _parse_byte_range(range_header: Optional[str], total_length: Optional[int]):
    if not range_header:
        return None

    value = range_header.strip().lower()
    if not value.startswith("bytes="):
        return None

    spec = value.split("=", 1)[1].strip()
    if "," in spec or "-" not in spec:
        return None

    start_text, end_text = spec.split("-", 1)
    start_text = start_text.strip()
    end_text = end_text.strip()

    if not start_text:
        if total_length is None or not end_text.isdigit():
            return None
        suffix_length = int(end_text)
        if suffix_length <= 0:
            return None
        start = max(total_length - suffix_length, 0)
        end = total_length - 1
        return start, end

    if not start_text.isdigit():
        return None

    start = int(start_text)
    end = None
    if end_text:
        if not end_text.isdigit():
            return None
        end = int(end_text)

    if total_length is not None:
        if start >= total_length:
            return None
        if end is None or end >= total_length:
            end = total_length - 1

    if end is not None and end < start:
        return None

    return start, end

def _record_prepare_metric(video_id: str, metrics: dict):
    with prepare_metrics_lock:
        prepare_metrics.append({
            "video_id": video_id,
            "resolve_ms": metrics.get("resolve_ms") or 0,
            "chunk_ms": metrics.get("chunk_ms") or 0,
            "server_ms": metrics.get("server_ms") or 0,
            "cached_prefix_bytes": metrics.get("cached_prefix_bytes") or 0,
            "resolve_cache_hit": bool(metrics.get("resolve_cache_hit")),
            "chunk_cache_hit": bool(metrics.get("chunk_cache_hit")),
            "active_queue": bool(metrics.get("active_queue")),
            "created_at": time.time(),
        })

def _summarize_prepare_metrics():
    with prepare_metrics_lock:
        items = list(prepare_metrics)

    if not items:
        return {
            "sample_count": 0,
            "avg_resolve_ms": 0,
            "avg_chunk_ms": 0,
            "avg_server_ms": 0,
            "avg_cached_prefix_bytes": 0,
            "resolve_cache_hit_rate": 0.0,
            "chunk_cache_hit_rate": 0.0,
        }

    count = len(items)
    return {
        "sample_count": count,
        "avg_resolve_ms": int(sum(item["resolve_ms"] for item in items) / count),
        "avg_chunk_ms": int(sum(item["chunk_ms"] for item in items) / count),
        "avg_server_ms": int(sum(item["server_ms"] for item in items) / count),
        "avg_cached_prefix_bytes": int(
            sum(item["cached_prefix_bytes"] for item in items) / count
        ),
        "resolve_cache_hit_rate": round(
            sum(1 for item in items if item["resolve_cache_hit"]) / count, 3
        ),
        "chunk_cache_hit_rate": round(
            sum(1 for item in items if item["chunk_cache_hit"]) / count, 3
        ),
    }

def _warm_initial_stream_chunk(video_id: str, stream_info: dict, target_bytes: int):
    target_bytes = max(target_bytes, STREAM_WARM_CHUNK_BYTES)
    while True:
        cached = _get_cached_stream_chunk(video_id, min_bytes=target_bytes)
        if cached is not None:
            return cached

        with stream_chunk_inflight_lock:
            pending = stream_chunk_inflight.get(video_id)
            if pending is None:
                pending = Future()
                stream_chunk_inflight[video_id] = pending
                should_fetch = True
            else:
                should_fetch = False

        if not should_fetch:
            try:
                pending.result(timeout=25)
            except Exception:
                pass
            continue

        try:
            existing = _get_cached_stream_chunk(video_id)
            existing_bytes = existing.get("bytes") if existing else b""
            total_length = existing.get("total_length") if existing else None
            if total_length is not None and len(existing_bytes) >= total_length:
                pending.set_result(existing)
                return existing

            headers = dict(stream_info["headers"])
            start_offset = len(existing_bytes)
            headers["range"] = f"bytes={start_offset}-{max(target_bytes - 1, start_offset)}"
            req = upstream_http.get(
                stream_info["url"],
                headers=headers,
                stream=True,
                timeout=(5, 20),
            )
            try:
                req.raise_for_status()
                chunks = [existing_bytes] if existing_bytes else []
                total_bytes = len(existing_bytes)
                for chunk in req.iter_content(chunk_size=1024 * 64):
                    if not chunk:
                        continue
                    remaining = target_bytes - total_bytes
                    if remaining <= 0:
                        break
                    if len(chunk) > remaining:
                        chunk = chunk[:remaining]
                    chunks.append(chunk)
                    total_bytes += len(chunk)
                    if total_bytes >= target_bytes:
                        break

                payload = {
                    "bytes": b"".join(chunks),
                    "content_type": req.headers.get(
                        "content-type",
                        stream_info.get("mime_type") or "audio/mp4",
                    ),
                    "total_length": _extract_total_length(req.headers) or total_length,
                }
                _store_stream_chunk(video_id, payload)
                pending.set_result(payload)
                return payload
            finally:
                req.close()
        except Exception as exc:
            pending.set_exception(exc)
            raise
        finally:
            with stream_chunk_inflight_lock:
                if stream_chunk_inflight.get(video_id) is pending:
                    stream_chunk_inflight.pop(video_id, None)

def _prepare_stream_track(video_id: str, target_chunk_bytes: int, active_queue: bool):
    total_start = time.perf_counter()
    resolve_start = time.perf_counter()
    resolve_cache_hit = _is_stream_info_cached(video_id)
    stream_info = get_stream_info(video_id)
    resolve_ms = int((time.perf_counter() - resolve_start) * 1000)

    chunk_cache_hit = _get_cached_stream_chunk(video_id, min_bytes=target_chunk_bytes) is not None
    chunk_start = time.perf_counter()
    chunk_payload = _warm_initial_stream_chunk(video_id, stream_info, target_chunk_bytes)
    chunk_ms = int((time.perf_counter() - chunk_start) * 1000)

    metrics = {
        "prepared": True,
        "playback_path": f"/proxy_stream/{video_id}",
        "resolve_cache_hit": resolve_cache_hit,
        "chunk_cache_hit": chunk_cache_hit,
        "resolve_ms": resolve_ms,
        "chunk_ms": chunk_ms,
        "target_chunk_bytes": target_chunk_bytes,
        "cached_prefix_bytes": len(chunk_payload.get("bytes") or b""),
        "duration": stream_info.get("duration") or 0,
        "active_queue": active_queue,
        "server_ms": int((time.perf_counter() - total_start) * 1000),
    }
    _record_prepare_metric(video_id, metrics)
    return metrics

def _prepare_stream_track_safely(video_id: str, target_chunk_bytes: int, active_queue: bool):
    try:
        return _prepare_stream_track(video_id, target_chunk_bytes, active_queue)
    except Exception:
        return None


def _classify_stream_failure(exc: Exception):
    message = str(exc or "").strip()
    lowered = message.lower()
    if "video unavailable" in lowered or "this video is not available" in lowered:
        return {
            "code": "video_unavailable",
            "message": message or "Video unavailable",
            "status_code": 410,
        }
    if "requested format is not available" in lowered:
        return {
            "code": "format_unavailable",
            "message": message or "Requested format is not available",
            "status_code": 410,
        }
    if "sign in to confirm" in lowered or "not a bot" in lowered:
        return {
            "code": "source_blocked",
            "message": message or "Upstream source requires verification",
            "status_code": 502,
        }
    return {
        "code": "stream_failed",
        "message": message or exc.__class__.__name__,
        "status_code": 500,
    }


def _prepare_streams_with_failures(
    video_ids: List[str],
    limit: int = 18,
    current_video_id: Optional[str] = None,
    active_queue: bool = False,
):
    prepared = {}
    failed = {}
    deduped_ids = []
    seen = set()
    for video_id in video_ids:
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        deduped_ids.append(video_id)
        if len(deduped_ids) >= limit:
            break

    if not deduped_ids:
        return prepared, failed

    if current_video_id and current_video_id in deduped_ids:
        deduped_ids.remove(current_video_id)
        deduped_ids.insert(0, current_video_id)

    targets = {
        video_id: _chunk_target_bytes(index, active_queue)
        for index, video_id in enumerate(deduped_ids)
    }

    max_workers = min(PREPARE_SESSION_WORKERS, len(deduped_ids))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _prepare_stream_track,
                video_id,
                targets[video_id],
                active_queue,
            ): video_id
            for video_id in deduped_ids
        }
        for future, video_id in future_map.items():
            try:
                prepared[video_id] = future.result(timeout=25)
            except Exception as exc:
                failed[video_id] = _classify_stream_failure(exc)
    return prepared, failed

def _prepare_streams(
    video_ids: List[str],
    limit: int = 18,
    current_video_id: Optional[str] = None,
    active_queue: bool = False,
):
    prepared, _ = _prepare_streams_with_failures(
        video_ids,
        limit=limit,
        current_video_id=current_video_id,
        active_queue=active_queue,
    )
    return prepared

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

def refresh_stream_info(video_id: str):
    with stream_info_lock:
        stream_info_cache.pop(video_id, None)
    return get_stream_info(video_id)

def _should_refresh_stream_info(exc: Exception):
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code in {401, 403, 404, 410, 416}

def _iter_upstream_stream(video_id: str, stream_info: dict, start: int = 0, end: Optional[int] = None):
    current_start = max(start, 0)
    current_stream_info = stream_info
    attempts = 0
    refreshed = False

    while True:
        req = None
        try:
            headers = dict(current_stream_info["headers"])
            if current_start > 0 or end is not None:
                if end is None:
                    headers["range"] = f"bytes={current_start}-"
                else:
                    headers["range"] = f"bytes={current_start}-{end}"

            req = upstream_http.get(
                current_stream_info["url"],
                headers=headers,
                stream=True,
                timeout=(8, 90),
            )
            req.raise_for_status()

            for chunk in req.iter_content(chunk_size=1024 * 64):
                if not chunk:
                    continue
                yield chunk
                current_start += len(chunk)
                if end is not None and current_start > end:
                    return
            return
        except Exception as exc:
            if attempts >= 2:
                raise
            attempts += 1
            if _should_refresh_stream_info(exc) and not refreshed:
                refreshed = True
                try:
                    current_stream_info = refresh_stream_info(video_id)
                except Exception:
                    raise exc
            time.sleep(0.15)
        finally:
            if req is not None:
                req.close()

def _warm_stream_safely(video_id: str):
    _prepare_stream_track_safely(
        video_id,
        _chunk_target_bytes(4, False),
        False,
    )

def queue_stream_warmup(video_ids: List[str], limit: int = 18):
    seen = set()
    for video_id in video_ids:
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        stream_warm_executor.submit(_warm_stream_safely, video_id)
        if len(seen) >= limit:
            break

@app.post("/prepare_session")
def prepare_session(req: WarmStreamRequest):
    start_time = time.perf_counter()
    limit = min(max(req.lookahead or len(req.video_ids), 1), PREPARE_SESSION_MAX_LOOKAHEAD)
    prepared, failed = _prepare_streams_with_failures(
        req.video_ids,
        limit=limit,
        current_video_id=req.current_video_id,
        active_queue=req.active_queue,
    )
    return {
        "status": "success",
        "prepared": prepared,
        "prepared_ids": list(prepared.keys()),
        "failed": failed,
        "failed_ids": list(failed.keys()),
        "summary": _summarize_prepare_metrics(),
        "server_ms": int((time.perf_counter() - start_time) * 1000),
    }

@app.get("/latency_summary")
def latency_summary():
    return {
        "status": "success",
        "summary": _summarize_prepare_metrics(),
        "stream_info_cache_size": len(stream_info_cache),
        "stream_chunk_cache_size": len(stream_chunk_cache),
    }

@app.get("/")
def health_check():
    return {"status": "Auralis Python Proxy is running"}

@app.post("/track_details")
def get_track_details(req: DownloadRequest):
    try:
        return _build_track_details_payload(req.video_id)
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

    results = _ytmusic_song_search(query, req.limit)
    if not results:
        results = _ytdlp_song_search(query, req.limit)

    return {"status": "success", "results": results}

@app.post("/search_albums")
def search_albums(req: SearchRequest):
    try:
        raw_results = ytmusic.search(req.query, filter="albums", limit=req.limit)
        albums = normalize_album_results(raw_results)
        if not albums:
            try:
                fallback_results = ytmusic.search(req.query, limit=max(req.limit * 3, 12))
            except Exception:
                fallback_results = []
            albums = normalize_album_results(fallback_results)
        return {"status": "success", "albums": albums}
    except Exception as e:
        return {"status": "success", "albums": []}

@app.get("/album/{album_id}")
def get_album_details(album_id: str):
    try:
        return _build_album_details_payload(album_id)
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

def _fallback_home_candidates(limit: int):
    now = time.time()
    with home_candidates_lock:
        cached = home_candidates_cache.get("results") or []
        if home_candidates_cache.get("expires_at", 0) > now and len(cached) >= max(5, limit):
            return cached[: limit + 10]

    results = []
    try:
        home_feed = ytmusic.get_home(limit=limit)
    except Exception:
        home_feed = []

    for carousel in home_feed:
        for item in carousel.get("contents", []):
            track = normalize_recommendation_track(item)
            if track is None:
                continue
            results.append((track, 1.0))
            if len(results) >= limit + 10:
                return results

    if len(results) < 5:
        try:
            backup = ytmusic.search("trending hit songs", filter="songs", limit=limit)
        except Exception:
            backup = []
        for entry in backup:
            track = normalize_recommendation_track(entry)
            if track is None:
                continue
            results.append((track, 0.7))
            if len(results) >= limit + 10:
                break

    if results:
        with home_candidates_lock:
            home_candidates_cache["results"] = results[: limit + 10]
            home_candidates_cache["expires_at"] = now + RECOMMENDATION_CACHE_TTL_SECONDS
    return results

def _recommendation_cache_key(req: SearchRequest):
    payload = {
        "query": (req.query or "").strip().lower(),
        "limit": int(req.limit),
        "offset": max(int(req.offset or 0), 0),
        "seed_id": (req.seed_id or "").strip(),
        "seed_ids": [item for item in (req.seed_ids or []) if item][:6],
        "artist_hints": [item.strip().lower() for item in (req.artist_hints or []) if item][:6],
        "taste_queries": [item.strip().lower() for item in (req.taste_queries or []) if item][:8],
        "avoid_ids": [item for item in (req.avoid_ids or []) if item][:40],
    }
    return json.dumps(payload, sort_keys=True)

def _get_cached_recommendations(cache_key: str):
    now = time.time()
    with recommendation_cache_lock:
        cached = recommendation_cache.get(cache_key)
        if cached and cached["expires_at"] > now:
            return cached["results"]
        if cached:
            recommendation_cache.pop(cache_key, None)
    return None

def _set_cached_recommendations(cache_key: str, results):
    with recommendation_cache_lock:
        recommendation_cache[cache_key] = {
            "results": results,
            "expires_at": time.time() + RECOMMENDATION_CACHE_TTL_SECONDS,
        }


def _recommendation_candidate_window(req: SearchRequest) -> int:
    limit = max(int(req.limit or 0), 1)
    offset = max(int(req.offset or 0), 0)
    avoid_count = len([item for item in (req.avoid_ids or []) if item])
    bias = max(offset, avoid_count)
    return min(max(limit + bias + 8, limit + 8), 72)

def _rank_recommendation_candidates(
    candidates,
    *,
    limit: int,
    avoid_ids,
    seed_ids,
    artist_hints,
    taste_queries,
):
    ranked = {}
    blocked_ids = {item for item in avoid_ids if item}.union(
        {item for item in seed_ids if item}
    )
    normalized_artist_hints = [
        _normalize_text(item) for item in artist_hints if _normalize_text(item)
    ]
    query_token_groups = [
        tokens[:4]
        for tokens in (_query_tokens(item) for item in taste_queries)
        if tokens
    ]

    for track, base_score in candidates:
        track_id = track.get("id")
        if not track_id or track_id in blocked_ids:
            continue
        title_text = _normalize_text(track.get("title"))
        artist_text = _normalize_text(track.get("channel"))
        album_text = _normalize_text(track.get("album"))
        score = float(base_score)

        for index, artist_hint in enumerate(normalized_artist_hints):
            if artist_hint and artist_hint in artist_text:
                score += max(3.2 - (index * 0.35), 0.8)

        for index, tokens in enumerate(query_token_groups):
            hits = sum(
                1
                for token in tokens
                if token in title_text or token in artist_text or token in album_text
            )
            if hits:
                score += min(hits, 2) * max(2.1 - (index * 0.18), 0.45)

        existing = ranked.get(track_id)
        if existing is None or score > existing["score"]:
            ranked[track_id] = {
                "track": track,
                "score": score,
                "artist_key": artist_text,
            }

    ordered = sorted(
        ranked.values(),
        key=lambda item: (item["score"], item["track"].get("title") or ""),
        reverse=True,
    )

    results = []
    artist_counts = {}
    for item in ordered:
        artist_key = item["artist_key"]
        if artist_key:
            artist_count = artist_counts.get(artist_key, 0)
            if artist_count >= 2 and len(results) + 1 < limit:
                continue
            artist_counts[artist_key] = artist_count + 1
        results.append(item["track"])
        if len(results) >= limit:
            break
    return results

@app.post("/recommend")
def get_recommendations(req: SearchRequest):
    """
    Builds recommendations from multiple explicit taste signals instead of
    relying on only the latest played seed.
    """
    try:
        cache_key = _recommendation_cache_key(req)
        cached_results = _get_cached_recommendations(cache_key)
        if cached_results is not None:
            return {"status": "success", "recommendations": cached_results}

        candidate_window = _recommendation_candidate_window(req)
        page_limit = max(int(req.limit or 0), 1)
        seed_ids = []
        for candidate in [req.seed_id, *(req.seed_ids or [])]:
            if candidate and candidate not in seed_ids:
                seed_ids.append(candidate)

        candidates = []

        for index, seed_id in enumerate(seed_ids[:3]):
            if not seed_id or seed_id == "trending hit songs":
                continue
            try:
                mix = ytmusic.get_watch_playlist(
                    videoId=seed_id,
                    limit=max(candidate_window, 10) + 6,
                )
            except Exception:
                continue

            source_weight = max(6.0 - index, 2.0)
            collected = 0
            for track in mix.get("tracks", []):
                normalized = normalize_recommendation_track(track)
                if normalized is None or normalized["id"] == seed_id:
                    continue
                candidates.append((normalized, source_weight))
                collected += 1
                if collected >= max(candidate_window // 2, 8):
                    break

        if len(candidates) < max(candidate_window, 12):
            for index, artist_hint in enumerate(req.artist_hints[:3]):
                query = (artist_hint or "").strip()
                if not query:
                    continue
                try:
                    results = ytmusic.search(
                        f"{query} songs",
                        filter="songs",
                        limit=min(max(candidate_window // 2, 6), 24),
                    )
                except Exception:
                    continue

                source_weight = max(3.8 - (index * 0.3), 1.3)
                for track in results:
                    normalized = normalize_recommendation_track(track)
                    if normalized is not None:
                        candidates.append((normalized, source_weight))

        if len(candidates) < max(candidate_window, 12) and not seed_ids:
            for index, taste_query in enumerate(req.taste_queries[:2]):
                query = (taste_query or "").strip()
                if not query:
                    continue
                try:
                    results = ytmusic.search(
                        query,
                        filter="songs",
                        limit=min(max(candidate_window // 2, 5), 20),
                    )
                except Exception:
                    continue

                source_weight = max(2.7 - (index * 0.25), 0.9)
                for track in results:
                    normalized = normalize_recommendation_track(track)
                    if normalized is not None:
                        candidates.append((normalized, source_weight))

        if len(candidates) < max(candidate_window, 12):
            candidates.extend(_fallback_home_candidates(candidate_window))

        ranked_results = _rank_recommendation_candidates(
            candidates,
            limit=page_limit + 1,
            avoid_ids=req.avoid_ids or [],
            seed_ids=seed_ids,
            artist_hints=req.artist_hints or [],
            taste_queries=req.taste_queries or [],
        )
        has_more = len(ranked_results) > page_limit
        results = ranked_results[:page_limit]
        _set_cached_recommendations(cache_key, results)
        return {
            "status": "success",
            "recommendations": results,
            "has_more": has_more,
            "next_offset": max(int(req.offset or 0), 0) + len(results),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/assistant/sessions")
def assistant_list_sessions(user_scope_id: str, include_archived: bool = False):
    sessions = _assistant_list_sessions(user_scope_id, include_archived=include_archived)
    return {"status": "success", "sessions": sessions}


@app.post("/assistant/sessions")
def assistant_create_session(req: AssistantSessionCreateRequest):
    session = _assistant_create_session(req.user_scope_id, title=req.title)
    return {"status": "success", "session": session}


@app.get("/assistant/sessions/{session_id}")
def assistant_get_session(session_id: str, user_scope_id: str):
    detail = _assistant_get_session_detail(session_id, user_scope_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Assistant session not found")
    return {"status": "success", **detail}


@app.patch("/assistant/sessions/{session_id}")
def assistant_update_session(session_id: str, req: AssistantSessionUpdateRequest):
    session = _assistant_update_session(
        session_id,
        req.user_scope_id,
        title=req.title,
        archived=req.archived,
        pinned=req.pinned,
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Assistant session not found")
    return {"status": "success", "session": session}


@app.delete("/assistant/sessions/{session_id}")
def assistant_delete_session(session_id: str, user_scope_id: str):
    deleted = _assistant_delete_session(session_id, user_scope_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Assistant session not found")
    return {"status": "success"}


@app.post("/assistant/chat")
def assistant_chat(req: AssistantChatRequest):
    session = None
    scope_id = _assistant_safe_scope_id(req.user_scope_id)
    try:
        request_started_at = time.perf_counter()
        if req.session_id:
            session = _assistant_get_session(req.session_id, scope_id)
        if session is None:
            session = _assistant_create_session(scope_id, seed_message=req.message)
            req.session_id = session["id"]

        _assistant_store_session_message(
            session["id"],
            scope_id,
            role="user",
            content=req.message,
            payload={"role": "user"},
        )
        _assistant_touch_session(
            session["id"],
            scope_id,
            last_message_preview=req.message,
        )

        selected_model = _assistant_model_for_request(req)
        if USE_LANGGRAPH_ASSISTANT and langgraph_runtime_available():
            payload = run_langgraph_assistant(req, _assistant_langgraph_deps(req))
        else:
            reply = _assistant_fallback_chat_reply(req, model_override=selected_model)
            _assistant_store_turn_memory(
                req,
                {
                    "reply": reply,
                    "action_type": "chat",
                    "selected_track_ids": [],
                    "playlist_name": None,
                    "playlist_summary": None,
                },
                selected_tracks=[],
                target_playlist=None,
            )
            payload = {
                "status": "success",
                "mode": "conversation",
                "reply": reply,
                "follow_up_question": None,
                "tracks": [],
                "playlist_draft": None,
                "target_playlist": None,
                "playlist_options": [],
                "fact_cards": [],
                "source_links": [],
                "clarification_options": [],
                "action_type": "chat",
            }
        diagnostics = payload.get("diagnostics")
        if isinstance(diagnostics, dict):
            diagnostics.setdefault("model", selected_model)
            diagnostics.setdefault(
                "total_http_ms",
                int((time.perf_counter() - request_started_at) * 1000),
            )
            payload["diagnostics"] = diagnostics
            print(
                "[assistant_chat] "
                f"session={session['id']} "
                f"mode={payload.get('mode')} "
                f"action={payload.get('action_type')} "
                f"model={diagnostics.get('model')} "
                f"planned={','.join(diagnostics.get('planned_tools') or []) or 'none'} "
                f"executed={','.join(diagnostics.get('executed_tools') or []) or 'none'} "
                f"totalMs={diagnostics.get('total_http_ms')}"
            )
        else:
            print(
                "[assistant_chat] "
                f"session={session['id']} "
                f"mode={payload.get('mode')} "
                f"action={payload.get('action_type')} "
                f"model={selected_model} "
                f"totalMs={int((time.perf_counter() - request_started_at) * 1000)}"
            )

        _assistant_store_session_message(
            session["id"],
            scope_id,
            role="assistant",
            content=payload.get("reply") or "",
            payload=payload,
        )
        _assistant_touch_session(
            session["id"],
            scope_id,
            last_message_preview=payload.get("reply") or req.message,
            last_mode=payload.get("mode"),
        )
        refreshed_session = _assistant_get_session(session["id"], scope_id) or session
        return {
            **payload,
            "session_id": refreshed_session["id"],
            "session_title": refreshed_session["title"],
            "session": refreshed_session,
        }
    except Exception as e:
        print("[assistant_chat][error]", traceback.format_exc())
        if session is not None:
            payload = {
                "status": "success",
                "mode": "conversation",
                "reply": "I hit a snag pulling that together, but I can keep going. Try rephrasing it or ask me to narrow the request.",
                "follow_up_question": None,
                "tracks": [],
                "playlist_draft": None,
                "target_playlist": None,
                "playlist_options": [],
                "fact_cards": [],
                "source_links": [],
                "clarification_options": [],
                "action_type": "chat",
                "diagnostics": {
                    "error": str(e),
                    "total_http_ms": 0,
                },
            }
            _assistant_store_session_message(
                session["id"],
                scope_id,
                role="assistant",
                content=payload["reply"],
                payload=payload,
            )
            _assistant_touch_session(
                session["id"],
                scope_id,
                last_message_preview=payload["reply"],
                last_mode=payload["mode"],
            )
            refreshed_session = _assistant_get_session(session["id"], scope_id) or session
            return {
                **payload,
                "session_id": refreshed_session["id"],
                "session_title": refreshed_session["title"],
                "session": refreshed_session,
            }
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/warm_streams")
def warm_streams(req: WarmStreamRequest):
    prepared, failed = _prepare_streams_with_failures(
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
        cached_chunk = _get_cached_stream_chunk(video_id)
        range_header = request.headers.get("range")

        if cached_chunk:
            cached_bytes = cached_chunk.get("bytes") or b""
            content_type = cached_chunk.get("content_type") or stream_info.get("mime_type") or "audio/mp4"
            total_length = cached_chunk.get("total_length")
            parsed_range = _parse_byte_range(range_header, total_length)

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

                        yield from _iter_upstream_stream(
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
                        resp_headers["Content-Length"] = str(
                            max(end - start + 1, 0)
                        )

                    return StreamingResponse(
                        generate_cached_then_upstream(),
                        status_code=status_code,
                        headers=resp_headers,
                        media_type=content_type,
                    )

        total_length = cached_chunk.get("total_length") if cached_chunk else None
        parsed_range = _parse_byte_range(range_header, total_length)
        start = parsed_range[0] if parsed_range else 0
        end = parsed_range[1] if parsed_range else None

        def generate_upstream():
            yield from _iter_upstream_stream(
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
            media_type=stream_info.get("mime_type", "audio/mp4")
        )
    except HTTPException:
        raise
    except Exception as e:
        classified = _classify_stream_failure(e)
        raise HTTPException(
            status_code=classified["status_code"],
            detail={
                "code": classified["code"],
                "message": classified["message"],
                "video_id": video_id,
            },
        )

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
    except HTTPException:
        raise
    except Exception as e:
        classified = _classify_stream_failure(e)
        raise HTTPException(
            status_code=classified["status_code"],
            detail={
                "code": classified["code"],
                "message": classified["message"],
                "video_id": video_id,
            },
        )

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting Auralis Proxy Server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)



