from __future__ import annotations

import json
import time
import uuid
from email.parser import BytesParser
from email.policy import default
from typing import Any, Dict, Iterable, List, Tuple

from fastapi import HTTPException, Request

from ..domain.catalog import (
    normalize_album_title,
    normalize_artist_name,
    normalize_track_title,
)
from ..recommend.store_runtime import open_recommendation_store_connection_initialized
from ..search.runtime import search_tracks_direct
from .media import (
    MediaPreparationError,
    extract_recognition_windows,
    guess_media_kind,
    persist_upload_bytes,
    temporary_work_dir,
)
from .providers import (
    ACRCloudRecognitionProvider,
    ProviderRequestError,
    ProviderUnavailableError,
    RecognitionMatch,
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True)
    except Exception:
        return "{}"


def _match_identity(match: RecognitionMatch) -> str:
    return "|".join(
        [
            normalize_track_title(match.title),
            normalize_artist_name(match.artist),
        ]
    )


def _is_unofficial_artist(value: str) -> bool:
    normalized = normalize_artist_name(value)
    return any(
        token in normalized
        for token in ("tribute", "karaoke", "cover", "sound alike")
    )


class RecognitionService:
    _MAX_UPLOAD_BYTES = 32 * 1024 * 1024

    def __init__(self, server: Any) -> None:
        self._server = server
        self._provider = ACRCloudRecognitionProvider()

    async def recognize_audio(self, request: Request) -> Dict[str, Any]:
        request_id = str(uuid.uuid4())
        started_at = time.perf_counter()
        try:
            fields, file_part = await self._parse_multipart_request(request)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid multipart request: {exc}") from exc

        source_type = _clean_text(fields.get("source_type")) or "uploaded"
        client_filename = _clean_text(fields.get("filename")) or file_part.get("filename") or "upload.bin"
        mime_type = _clean_text(file_part.get("content_type")) or _clean_text(fields.get("mime_type"))
        media_kind = _clean_text(fields.get("media_kind")) or guess_media_kind(client_filename, mime_type)
        user_scope_id = _clean_text(fields.get("user_scope_id")) or "guest"
        session_id = _clean_text(fields.get("session_id"))
        if media_kind not in {"audio", "video"}:
            raise HTTPException(status_code=415, detail="Only audio and video uploads are supported")

        diagnostics: Dict[str, Any] = {
            "request_id": request_id,
            "source_type": source_type,
            "media_kind": media_kind,
            "upload_bytes": len(file_part["content"]),
        }

        try:
            payload = self._recognize_bytes(
                file_part["content"],
                filename=client_filename,
                mime_type=mime_type,
                source_type=source_type,
                user_scope_id=user_scope_id,
                session_id=session_id,
                diagnostics=diagnostics,
            )
        except ProviderUnavailableError as exc:
            payload = {
                "status": "success",
                "request_id": request_id,
                "recognition_status": "provider_unavailable",
                "provider": self._provider.provider_name,
                "confidence": 0.0,
                "recognized_metadata": None,
                "resolved_track": None,
                "alternatives": [],
                "diagnostics": {
                    **diagnostics,
                    "provider_error": str(exc),
                },
            }
        except HTTPException:
            raise
        except MediaPreparationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ProviderRequestError as exc:
            payload = {
                "status": "success",
                "request_id": request_id,
                "recognition_status": "failed",
                "provider": self._provider.provider_name,
                "confidence": 0.0,
                "recognized_metadata": None,
                "resolved_track": None,
                "alternatives": [],
                "diagnostics": {
                    **diagnostics,
                    "provider_error": str(exc),
                },
            }
        payload.setdefault("request_id", request_id)
        payload.setdefault("status", "success")
        payload["diagnostics"] = {
            **dict(payload.get("diagnostics") or {}),
            "total_ms": int((time.perf_counter() - started_at) * 1000),
        }
        return payload

    async def _parse_multipart_request(self, request: Request) -> Tuple[Dict[str, str], Dict[str, Any]]:
        content_type = _clean_text(request.headers.get("content-type"))
        if "multipart/form-data" not in content_type.lower():
            raise HTTPException(status_code=415, detail="Expected multipart/form-data upload")
        body = await request.body()
        if not body:
            raise HTTPException(status_code=400, detail="Request body is empty")
        if len(body) > self._MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Uploaded media exceeds the 32MB limit")
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
        )
        fields: Dict[str, str] = {}
        file_part: Dict[str, Any] | None = None
        for part in message.iter_parts():
            field_name = _clean_text(part.get_param("name", header="content-disposition"))
            if not field_name:
                continue
            payload = part.get_payload(decode=True) or b""
            filename = part.get_filename()
            if filename:
                if file_part is not None:
                    continue
                file_part = {
                    "field_name": field_name,
                    "filename": filename,
                    "content": payload,
                    "content_type": _clean_text(part.get_content_type()),
                }
            else:
                fields[field_name] = payload.decode(
                    part.get_content_charset() or "utf-8",
                    errors="ignore",
                ).strip()
        if file_part is None:
            raise HTTPException(status_code=400, detail="Missing uploaded media part")
        return fields, file_part

    def _recognize_bytes(
        self,
        payload: bytes,
        *,
        filename: str,
        mime_type: str,
        source_type: str,
        user_scope_id: str,
        session_id: str,
        diagnostics: Dict[str, Any],
    ) -> Dict[str, Any]:
        with temporary_work_dir() as temp_dir:
            input_path = persist_upload_bytes(payload, filename=filename, temp_dir=temp_dir)
            windows, media_diagnostics = extract_recognition_windows(input_path, temp_dir=temp_dir)
            diagnostics.update(media_diagnostics)

            provider_started_at = time.perf_counter()
            window_matches: List[Tuple[Dict[str, Any], List[RecognitionMatch]]] = []
            for window in windows:
                matches = self._provider.identify_file(
                    window["path"],
                    mime_type=window.get("mime_type", "audio/wav"),
                )
                window_matches.append((window, matches))
            diagnostics["provider_ms"] = int((time.perf_counter() - provider_started_at) * 1000)

            aggregated = self._aggregate_matches(window_matches)
            if not aggregated:
                response = {
                    "status": "success",
                    "recognition_status": "no_match",
                    "provider": self._provider.provider_name,
                    "confidence": 0.0,
                    "recognized_metadata": None,
                    "resolved_track": None,
                    "alternatives": [],
                    "diagnostics": diagnostics,
                }
                self._store_recognition_event(
                    user_scope_id=user_scope_id,
                    source_type=source_type,
                    session_id=session_id,
                    recognition_status="no_match",
                    confidence=0.0,
                    recognized_metadata=None,
                    resolved_track=None,
                    diagnostics=diagnostics,
                )
                return response

            resolution_started_at = time.perf_counter()
            resolved_groups = [
                self._resolve_aggregated_match(aggregated_match)
                for aggregated_match in aggregated
            ]
            diagnostics["resolution_ms"] = int((time.perf_counter() - resolution_started_at) * 1000)
            resolved_groups = sorted(
                resolved_groups,
                key=lambda item: (
                    float(item.get("resolution_score") or 0.0),
                    float(item.get("confidence") or 0.0),
                    int(item.get("window_hits") or 0),
                ),
                reverse=True,
            )

            best = resolved_groups[0]
            second = resolved_groups[1] if len(resolved_groups) > 1 else None
            top_margin = float(best.get("resolution_score") or 0.0) - float(
                (second or {}).get("resolution_score") or 0.0
            )
            strong_match = (
                best.get("resolved_track") is not None
                and float(best.get("resolution_score") or 0.0) >= 11.0
                and (
                    top_margin >= 1.5
                    or float(best.get("confidence") or 0.0) >= 92.0
                    or int(best.get("window_hits") or 0) >= 2
                )
            )
            recognition_status = "resolved" if strong_match else "ambiguous"
            alternatives = resolved_groups[:5]
            response = {
                "status": "success",
                "recognition_status": recognition_status,
                "provider": self._provider.provider_name,
                "confidence": float(best.get("confidence") or 0.0),
                "recognized_metadata": dict(best.get("recognized_metadata") or {}),
                "resolved_track": best.get("resolved_track") if strong_match else None,
                "alternatives": alternatives if not strong_match else alternatives[1:4],
                "diagnostics": {
                    **diagnostics,
                    "top_margin": top_margin,
                },
            }
            self._store_recognition_event(
                user_scope_id=user_scope_id,
                source_type=source_type,
                session_id=session_id,
                recognition_status=recognition_status,
                confidence=float(best.get("confidence") or 0.0),
                recognized_metadata=dict(best.get("recognized_metadata") or {}),
                resolved_track=best.get("resolved_track") if strong_match else None,
                diagnostics=response["diagnostics"],
            )
            return response

    def _aggregate_matches(
        self,
        window_matches: Iterable[Tuple[Dict[str, Any], List[RecognitionMatch]]],
    ) -> List[Dict[str, Any]]:
        grouped: Dict[str, Dict[str, Any]] = {}
        for window, matches in window_matches:
            for match in matches:
                identity = _match_identity(match)
                if not identity.strip():
                    continue
                bucket = grouped.setdefault(
                    identity,
                    {
                        "recognized_metadata": {
                            "title": match.title,
                            "artist": match.artist,
                            "album": match.album,
                            "duration_ms": match.duration_ms,
                        },
                        "confidence_total": 0.0,
                        "window_hits": 0,
                        "provider_matches": [],
                    },
                )
                bucket["confidence_total"] += float(match.confidence or 0.0)
                bucket["window_hits"] += 1
                bucket["provider_matches"].append(
                    {
                        "title": match.title,
                        "artist": match.artist,
                        "album": match.album,
                        "confidence": float(match.confidence or 0.0),
                        "duration_ms": match.duration_ms,
                        "window_start_seconds": window.get("start_seconds"),
                        "window_duration_seconds": window.get("duration_seconds"),
                    }
                )
        aggregated: List[Dict[str, Any]] = []
        for identity, bucket in grouped.items():
            window_hits = int(bucket.get("window_hits") or 0)
            confidence_total = float(bucket.get("confidence_total") or 0.0)
            blended_confidence = min(
                100.0,
                round((confidence_total / max(window_hits, 1)) + ((window_hits - 1) * 9.0), 2),
            )
            aggregated.append(
                {
                    "identity": identity,
                    "recognized_metadata": dict(bucket.get("recognized_metadata") or {}),
                    "confidence": blended_confidence,
                    "window_hits": window_hits,
                    "provider_matches": list(bucket.get("provider_matches") or []),
                }
            )
        aggregated.sort(
            key=lambda item: (
                float(item.get("confidence") or 0.0),
                int(item.get("window_hits") or 0),
            ),
            reverse=True,
        )
        return aggregated

    def _resolve_aggregated_match(self, aggregated_match: Dict[str, Any]) -> Dict[str, Any]:
        metadata = dict(aggregated_match.get("recognized_metadata") or {})
        candidates = self._search_candidates_for_metadata(metadata)
        scored_candidates = sorted(
            (
                {
                    "track": candidate,
                    "score": self._score_resolved_track(candidate, metadata),
                }
                for candidate in candidates
            ),
            key=lambda item: float(item.get("score") or 0.0),
            reverse=True,
        )
        best_track = scored_candidates[0]["track"] if scored_candidates else None
        best_score = float(scored_candidates[0]["score"]) if scored_candidates else 0.0
        alternative_tracks = [
            {
                "resolved_track": dict(item["track"]),
                "resolution_score": float(item["score"]),
            }
            for item in scored_candidates[:4]
        ]
        return {
            **aggregated_match,
            "resolved_track": dict(best_track) if isinstance(best_track, dict) else None,
            "resolution_score": best_score,
            "resolved_track_candidates": alternative_tracks,
        }

    def _search_candidates_for_metadata(self, metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
        title = _clean_text(metadata.get("title"))
        artist = _clean_text(metadata.get("artist"))
        album = _clean_text(metadata.get("album"))
        queries = [
            " ".join(part for part in [title, artist] if part).strip(),
            " ".join(part for part in [title, artist, album] if part).strip(),
            title,
        ]
        seen = set()
        tracks: List[Dict[str, Any]] = []
        for query in queries:
            query = _clean_text(query)
            if not query:
                continue
            for track in search_tracks_direct(query, 8, server=self._server):
                if not isinstance(track, dict):
                    continue
                track_id = _clean_text(track.get("id"))
                track_key = track_id or json.dumps(
                    [
                        _clean_text(track.get("title")),
                        _clean_text(track.get("channel")),
                    ]
                )
                if track_key in seen:
                    continue
                seen.add(track_key)
                tracks.append(dict(track))
        return tracks

    def _score_resolved_track(self, track: Dict[str, Any], metadata: Dict[str, Any]) -> float:
        title_key = normalize_track_title(metadata.get("title"))
        artist_key = normalize_artist_name(metadata.get("artist"))
        album_key = normalize_album_title(metadata.get("album"))
        track_title_key = normalize_track_title(track.get("title") or track.get("name"))
        track_artist_key = normalize_artist_name(
            track.get("channel") or track.get("artist") or track.get("author")
        )
        track_album_key = normalize_album_title(track.get("album"))

        score = 0.0
        if title_key and track_title_key == title_key:
            score += 8.0
        elif title_key and (title_key in track_title_key or track_title_key in title_key):
            score += 4.0
        if artist_key and track_artist_key == artist_key:
            score += 7.0
        elif artist_key and (artist_key in track_artist_key or track_artist_key in artist_key):
            score += 3.5
        if album_key and track_album_key == album_key:
            score += 2.5
        elif album_key and track_album_key and (
            album_key in track_album_key or track_album_key in album_key
        ):
            score += 1.2
        if _is_unofficial_artist(track_artist_key):
            score -= 5.5
        duration_hint = int(metadata.get("duration_ms") or 0)
        track_duration = int(track.get("duration") or 0)
        if duration_hint > 0 and track_duration > 0:
            diff_ms = abs(duration_hint - (track_duration * 1000))
            if diff_ms <= 4000:
                score += 2.0
            elif diff_ms <= 9000:
                score += 1.0
            else:
                score -= 1.0
        return round(score, 3)

    def _store_recognition_event(
        self,
        *,
        user_scope_id: str,
        source_type: str,
        session_id: str,
        recognition_status: str,
        confidence: float,
        recognized_metadata: Dict[str, Any] | None,
        resolved_track: Dict[str, Any] | None,
        diagnostics: Dict[str, Any],
    ) -> None:
        try:
            with self._server.recommendation_store_lock:
                connection = open_recommendation_store_connection_initialized(self._server)
                try:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS recognition_match_events (
                            id TEXT PRIMARY KEY,
                            user_scope_id TEXT NOT NULL,
                            session_id TEXT,
                            source_type TEXT NOT NULL,
                            provider TEXT NOT NULL,
                            recognition_status TEXT NOT NULL,
                            confidence REAL NOT NULL,
                            recognized_title TEXT,
                            recognized_artist TEXT,
                            resolved_track_id TEXT,
                            payload_json TEXT,
                            occurred_at REAL NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO recognition_match_events(
                            id,
                            user_scope_id,
                            session_id,
                            source_type,
                            provider,
                            recognition_status,
                            confidence,
                            recognized_title,
                            recognized_artist,
                            resolved_track_id,
                            payload_json,
                            occurred_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            user_scope_id or "guest",
                            session_id or "",
                            source_type or "uploaded",
                            self._provider.provider_name,
                            recognition_status,
                            float(confidence or 0.0),
                            _clean_text((recognized_metadata or {}).get("title")),
                            _clean_text((recognized_metadata or {}).get("artist")),
                            _clean_text((resolved_track or {}).get("id")),
                            _safe_json(
                                {
                                    "recognized_metadata": recognized_metadata,
                                    "resolved_track": resolved_track,
                                    "diagnostics": diagnostics,
                                }
                            ),
                            float(time.time()),
                        ),
                    )
                    connection.commit()
                finally:
                    connection.close()
        except Exception:
            return
