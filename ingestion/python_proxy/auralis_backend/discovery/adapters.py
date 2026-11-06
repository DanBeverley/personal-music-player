from __future__ import annotations

import time
from typing import Any, Dict, List

from .config import ENGINE_MODEL_VERSION
from .schema import DiscoveryArtifact, DiscoveryRow


_CLIENT_TRACK_FIELDS = {
    "id",
    "track_key",
    "canonical_recording_id",
    "musicbrainz_recording_id",
    "isrc",
    "videoId",
    "video_id",
    "playback",
    "playback_source_id",
    "provider",
    "source_provider",
    "source_authority",
    "source_identity_authority",
    "title",
    "name",
    "channel",
    "artist",
    "author",
    "artists",
    "artist_id",
    "artist_ids",
    "artist_entities",
    "album",
    "album_title",
    "album_id",
    "browseId",
    "duration",
    "duration_seconds",
    "thumbnail",
    "thumbnails",
    "image",
    "year",
    "release_year",
    "release_date",
    "recommendation_reason",
    "relation_type",
    "relation_strength",
    "play_count",
    "last_played_at",
    "isHidden",
    "is_downloaded_locally",
    "download_path",
}

_CLIENT_ALBUM_FIELDS = {
    "id",
    "title",
    "name",
    "album",
    "artist",
    "channel",
    "artist_name",
    "thumbnail",
    "image",
    "year",
    "release_year",
    "release_date",
    "release_type",
    "browseId",
    "album_id",
    "musicbrainz_release_group_id",
    "musicbrainz_artist_id",
    "musicbrainz_artist_ids",
    "canonical_album_identity",
    "track_count",
    "canonical_track_count",
    "playable_coverage",
    "album_source",
    "genres",
    "genre",
    "subgenre",
    "language",
    "region",
    "relation_type",
    "relation_strength",
    "album_relation_reason",
    "album_relation_score",
    "recommendation_reason",
}

_CLIENT_ARTIST_FIELDS = {
    "id",
    "artist_id",
    "musicbrainz_artist_id",
    "name",
    "title",
    "artist",
    "thumbnail",
    "image",
    "genres",
    "genre",
    "relation_type",
    "relation_strength",
    "recommendation_reason",
}


def _copy_client_fields(item: Dict[str, Any], fields: set[str]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in item.items()
        if key in fields and value is not None
    }


def _track_to_client_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    return _copy_client_fields(item, _CLIENT_TRACK_FIELDS)


def _album_to_client_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    payload = _copy_client_fields(item, _CLIENT_ALBUM_FIELDS)
    raw_tracks = item.get("tracks") or item.get("canonical_tracks") or []
    if isinstance(raw_tracks, list) and raw_tracks:
        payload["tracks"] = [
            _track_to_client_payload(track)
            for track in raw_tracks
            if isinstance(track, dict)
        ]
    return payload


def _collection_to_client_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        key: value
        for key, value in item.items()
        if key not in {"tracks", "items", "recommendations", "canonical_tracks"}
    }
    raw_tracks = (
        item.get("tracks")
        or item.get("items")
        or item.get("recommendations")
        or []
    )
    if isinstance(raw_tracks, list):
        payload["tracks"] = [
            _track_to_client_payload(track)
            for track in raw_tracks
            if isinstance(track, dict)
        ]
    return payload


def _item_to_client_payload(item: Dict[str, Any], *, item_type: str) -> Dict[str, Any]:
    normalized_type = str(item_type or "track").strip().lower()
    if normalized_type == "album":
        return _album_to_client_payload(item)
    if normalized_type == "artist":
        return _copy_client_fields(item, _CLIENT_ARTIST_FIELDS)
    if normalized_type in {"mix", "radio"}:
        return _collection_to_client_payload(item)
    return _track_to_client_payload(item)


def _home_tab_lanes_to_client_payload(raw_lanes: Any) -> Dict[str, Any]:
    if not isinstance(raw_lanes, dict):
        return {}
    lanes: Dict[str, Any] = {}
    for lane_id, raw_lane in raw_lanes.items():
        if not isinstance(raw_lane, dict):
            continue
        lane: Dict[str, Any] = {}
        for collection, item_type in (
            ("tracks", "track"),
            ("discoveries", "track"),
            ("albums", "album"),
            ("artists", "artist"),
        ):
            raw_items = raw_lane.get(collection)
            if not isinstance(raw_items, list):
                continue
            lane[collection] = [
                _item_to_client_payload(item, item_type=item_type)
                for item in raw_items
                if isinstance(item, dict)
            ]
        lanes[str(lane_id)] = lane
    return lanes


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
    raw_page = items[start : start + page_limit] if page_limit else items[start:]
    page = [
        _item_to_client_payload(item, item_type=row.item_type)
        for item in raw_page
        if isinstance(item, dict)
    ]
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
    diagnostics = dict(artifact.diagnostics or {})
    diagnostics["home_tab_lanes"] = _home_tab_lanes_to_client_payload(
        diagnostics.get("home_tab_lanes"),
    )
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
        "feed_version": int(diagnostics.get("feed_version") or 0),
        "feed_action": str(diagnostics.get("feed_action") or ""),
        "preparation_state": str(diagnostics.get("preparation_state") or "idle"),
        "quality_warnings": list(diagnostics.get("quality_warnings") or []),
        "generated_at": artifact.generated_at,
        "expires_at": artifact.expires_at,
        "model_version": ENGINE_MODEL_VERSION,
        "rows": rows,
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
            diagnostics["home_tab_lanes"] = _home_tab_lanes_to_client_payload(
                diagnostics.get("home_tab_lanes"),
            )
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
                "has_more": bool(payload.get("has_more")),
                "next_offset": int(payload.get("next_offset") or 0),
                "diagnostics": diagnostics,
            }
    return None
