from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple


class MediaPreparationError(RuntimeError):
    pass


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _python_proxy_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_binary(binary_name: str) -> str:
    env_name = f"AURALIS_{binary_name.upper()}_BINARY"
    configured = _clean_text(os.environ.get(env_name))
    if configured:
        return configured
    root = _python_proxy_root()
    candidate_names = [binary_name]
    if os.name == "nt":
        candidate_names.insert(0, f"{binary_name}.exe")
    for candidate in candidate_names:
        bundled = root / candidate
        if bundled.exists():
            return str(bundled)
    discovered = shutil.which(binary_name)
    if discovered:
        return discovered
    raise MediaPreparationError(f"Required binary '{binary_name}' was not found")


def guess_media_kind(filename: str = "", mime_type: str = "") -> str:
    mime = _clean_text(mime_type).lower()
    name = _clean_text(filename).lower()
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    guessed_mime, _ = mimetypes.guess_type(name)
    if guessed_mime:
        if guessed_mime.startswith("video/"):
            return "video"
        if guessed_mime.startswith("audio/"):
            return "audio"
    video_exts = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
    audio_exts = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".opus", ".webm"}
    suffix = Path(name).suffix.lower()
    if suffix in video_exts:
        return "video"
    if suffix in audio_exts:
        return "audio"
    return ""


def probe_media(input_path: str) -> Dict[str, Any]:
    ffprobe = _resolve_binary("ffprobe")
    command = [
        ffprobe,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        input_path,
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MediaPreparationError(
            f"ffprobe failed: {(result.stderr or result.stdout or '').strip()[:220]}"
        )
    try:
        payload = json.loads(result.stdout or "{}")
    except Exception as exc:
        raise MediaPreparationError("ffprobe returned invalid JSON") from exc
    streams = list(payload.get("streams") or [])
    format_payload = payload.get("format") or {}
    duration_seconds = 0.0
    try:
        duration_seconds = float(format_payload.get("duration") or 0.0)
    except Exception:
        duration_seconds = 0.0
    has_audio = any(str(stream.get("codec_type") or "") == "audio" for stream in streams)
    has_video = any(str(stream.get("codec_type") or "") == "video" for stream in streams)
    return {
        "duration_seconds": duration_seconds,
        "has_audio": has_audio,
        "has_video": has_video,
        "format_name": _clean_text(format_payload.get("format_name")),
    }


def choose_recognition_windows(duration_seconds: float) -> List[Tuple[float, float]]:
    duration = max(0.0, float(duration_seconds or 0.0))
    if duration <= 0.0:
        return [(0.0, 12.0)]
    if duration <= 15.0:
        return [(0.0, max(6.0, duration))]
    if duration <= 35.0:
        start = max(0.0, (duration - 12.0) / 2.0)
        return [(round(start, 2), 12.0)]
    if duration <= 90.0:
        starts = [
            max(0.0, (duration * 0.38) - 6.0),
            max(0.0, (duration * 0.58) - 6.0),
        ]
    else:
        starts = [
            max(0.0, (duration * 0.33) - 6.0),
            max(0.0, (duration * 0.52) - 6.0),
            max(0.0, (duration * 0.7) - 6.0),
        ]
    deduped: List[Tuple[float, float]] = []
    seen = set()
    for start in starts:
        safe_start = min(start, max(0.0, duration - 12.0))
        key = round(safe_start, 1)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((round(safe_start, 2), 12.0))
    return deduped or [(0.0, min(duration, 12.0))]


def extract_recognition_windows(
    input_path: str,
    *,
    temp_dir: str,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    probe = probe_media(input_path)
    if not probe.get("has_audio"):
        raise MediaPreparationError("The uploaded media does not contain an audio stream")
    ffmpeg = _resolve_binary("ffmpeg")
    windows = choose_recognition_windows(float(probe.get("duration_seconds") or 0.0))
    extracted: List[Dict[str, Any]] = []
    started_at = time.perf_counter()
    for index, (start, clip_duration) in enumerate(windows):
        output_path = os.path.join(temp_dir, f"recognition_window_{index}.wav")
        command = [
            ffmpeg,
            "-y",
            "-ss",
            f"{start:.2f}",
            "-t",
            f"{clip_duration:.2f}",
            "-i",
            input_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-acodec",
            "pcm_s16le",
            output_path,
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not os.path.exists(output_path):
            raise MediaPreparationError(
                f"ffmpeg failed to normalize audio: {(result.stderr or result.stdout or '').strip()[:220]}"
            )
        extracted.append(
            {
                "path": output_path,
                "start_seconds": start,
                "duration_seconds": clip_duration,
                "mime_type": "audio/wav",
            }
        )
    return (
        extracted,
        {
            "normalization_ms": int((time.perf_counter() - started_at) * 1000),
            "window_count": len(extracted),
            "source_duration_seconds": float(probe.get("duration_seconds") or 0.0),
            "source_has_video": bool(probe.get("has_video")),
        },
    )


def persist_upload_bytes(
    payload: bytes,
    *,
    filename: str,
    temp_dir: str,
) -> str:
    safe_name = Path(filename or "upload.bin").name
    suffix = Path(safe_name).suffix or ".bin"
    destination = Path(temp_dir) / f"input{suffix}"
    destination.write_bytes(payload)
    return str(destination)


def temporary_work_dir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix="auralis_song_match_")
