from __future__ import annotations

import time
from typing import Any, Dict, List

from .config import ENGINE_MODEL_VERSION
from .schema import DiscoveryArtifact, DiscoveryRow


def _rotate_featured_items(items: List[Dict[str, Any]], *, window_seconds: int = 300) -> List[Dict[str, Any]]:
    if len(items) <= 1:
        return items
    offset = int(time.time() // max(window_seconds, 1)) % len(items)
    return [*items[offset:], *items[:offset]]


def row_to_payload(row: DiscoveryRow, *, offset: int = 0, limit: int | None = None) -> Dict[str, Any]:
    items = list(row.items or [])
    if row.kind == "featured_new_albums":
        items = _rotate_featured_items(items)
    start = max(int(offset or 0), 0)
    page_limit = max(int(limit or 0), 0) if limit is not None else len(items)
    page = items[start : start + page_limit] if page_limit else items[start:]
    next_offset = start + len(page)
    has_more = bool(row.has_more and next_offset < len(items))
    payload = {
        "id": row.id,
        "title": row.title,
        "kind": row.kind,
        "item_type": row.item_type,
        "items": page,
        "row_style": row.row_style,
        "next_offset": next_offset,
        "has_more": has_more,
    }
    if row.meta:
        payload["meta"] = dict(row.meta)
    return payload


def row_to_storage_payload(row: DiscoveryRow) -> Dict[str, Any]:
    payload = {
        "id": row.id,
        "title": row.title,
        "kind": row.kind,
        "item_type": row.item_type,
        "items": list(row.items or []),
        "row_style": row.row_style,
        "next_offset": int(row.next_offset or 0),
        "has_more": bool(row.has_more),
    }
    if row.meta:
        payload["meta"] = dict(row.meta)
    return payload


def rows_to_payload(rows: List[DiscoveryRow], *, page_size: int) -> List[Dict[str, Any]]:
    payloads: List[Dict[str, Any]] = []
    for row in rows:
        prepared_page_size = int((row.meta or {}).get("page_size") or page_size)
        limit = prepared_page_size if row.has_more else len(row.items or [])
        payloads.append(row_to_payload(row, offset=0, limit=limit))
    return payloads


def artifact_to_session(artifact: DiscoveryArtifact) -> Dict[str, Any]:
    return {
        "session_id": artifact.session_id,
        "user_scope_id": artifact.user_scope_id,
        "profile_key": artifact.profile_key,
        "generated_at": artifact.generated_at,
        "expires_at": artifact.expires_at,
        "rows": [row_to_storage_payload(row) for row in artifact.rows],
        "diagnostics": dict(artifact.diagnostics or {}),
    }


def home_response_from_artifact(
    artifact: DiscoveryArtifact,
    *,
    request_id: str,
    page_size: int,
) -> Dict[str, Any]:
    rows = rows_to_payload(artifact.rows, page_size=page_size)
    flattened: List[Dict[str, Any]] = []
    for row in rows:
        if str(row.get("item_type") or "track") != "track":
            continue
        flattened.extend(row.get("items") or [])
        if len(flattened) >= 32:
            break
    diagnostics = dict(artifact.diagnostics or {})
    diagnostics.setdefault("engine", "discovery_engine")
    diagnostics["artifact_source"] = artifact.artifact_source
    diagnostics.setdefault(
        "artifact_quality",
        "launchable" if artifact.accepted else "rejected",
    )
    diagnostics["cache_hit"] = artifact.artifact_source == "cache"
    diagnostics["ranking_backend"] = (
        "artifact_launch" if artifact.artifact_source == "cache" else "discovery_engine"
    )
    return {
        "status": "success",
        "request_id": request_id,
        "session_id": artifact.session_id,
        "generated_at": artifact.generated_at,
        "expires_at": artifact.expires_at,
        "model_version": ENGINE_MODEL_VERSION,
        "rows": rows,
        "shelves": rows,
        "recommendations": flattened[:32],
        "has_more": any(bool(row.get("has_more")) for row in rows),
        "next_offset": sum(len(row.get("items") or []) for row in rows),
        "diagnostics": diagnostics,
    }


def row_page_response_from_artifact(
    artifact: DiscoveryArtifact,
    *,
    row_id: str,
    offset: int,
    limit: int,
    request_id: str,
) -> Dict[str, Any] | None:
    for row in artifact.rows:
        if row.id == row_id or row.kind == row_id:
            payload = row_to_payload(row, offset=offset, limit=limit)
            diagnostics = dict(artifact.diagnostics or {})
            diagnostics.setdefault("engine", "discovery_engine")
            diagnostics["artifact_source"] = artifact.artifact_source
            diagnostics["cache_hit"] = artifact.artifact_source == "cache"
            diagnostics["ranking_backend"] = (
                "artifact_launch" if artifact.artifact_source == "cache" else "discovery_engine"
            )
            return {
                "status": "success",
                "request_id": request_id,
                "session_id": artifact.session_id,
                "generated_at": artifact.generated_at,
                "expires_at": artifact.expires_at,
                "model_version": ENGINE_MODEL_VERSION,
                "row": payload,
                "rows": [payload],
                "shelves": [payload],
                "recommendations": payload.get("items") or [],
                "has_more": bool(payload.get("has_more")),
                "next_offset": int(payload.get("next_offset") or 0),
                "diagnostics": diagnostics,
            }
    return None
