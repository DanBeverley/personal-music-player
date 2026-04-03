from __future__ import annotations

from collections import defaultdict
import json
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..storage.postgres import db_available, get_connection
from .feature_store import (
    SCENE_GRAPH_VERSION,
    ensure_feature_schema,
    hot_runtime_cache_get,
    hot_runtime_cache_put,
    persistent_store_reads_enabled,
    request_runtime_cache_get,
    request_runtime_cache_put,
)
from .store_runtime import open_recommendation_store_connection


def _json_loads(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _normalize_key(server: Any, graph_kind: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if str(graph_kind or "") == "scene_cluster":
        return text
    try:
        return server._normalize_text(text) or text
    except Exception:
        return text


def load_scene_graph_record(
    server: Any,
    *,
    graph_kind: str,
    graph_key: str,
) -> Optional[Dict[str, Any]]:
    normalized_kind = str(graph_kind or "").strip()
    normalized_key = _normalize_key(server, normalized_kind, graph_key)
    if not normalized_kind or not normalized_key:
        return None
    cache_key = f"{normalized_kind}:{normalized_key}"
    cached, payload = request_runtime_cache_get("scene_graph", cache_key)
    if cached:
        return payload
    cached, payload = hot_runtime_cache_get("scene_graph", cache_key)
    if cached:
        request_runtime_cache_put("scene_graph", cache_key, payload)
        return payload
    ensure_feature_schema(server)
    if persistent_store_reads_enabled() and db_available():
        try:
            with get_connection() as connection:
                if connection is not None:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT payload, graph_version
                            FROM recommendation_scene_graph_records
                            WHERE graph_kind = %s AND graph_key = %s
                            LIMIT 1
                            """,
                            [normalized_kind, normalized_key],
                        )
                        row = cursor.fetchone()
                        if row:
                            payload = _json_loads(row[0])
                            payload.setdefault("graph_kind", normalized_kind)
                            payload.setdefault("graph_key", normalized_key)
                            payload.setdefault("graph_version", row[1] or SCENE_GRAPH_VERSION)
                            hot_runtime_cache_put("scene_graph", cache_key, payload)
                            request_runtime_cache_put("scene_graph", cache_key, payload)
                            return payload
        except Exception:
            pass
    connection = open_recommendation_store_connection(server)
    try:
        row = connection.execute(
            """
            SELECT payload_json, graph_version
            FROM recommendation_scene_graph_records
            WHERE graph_kind = ? AND graph_key = ?
            LIMIT 1
            """,
            [normalized_kind, normalized_key],
        ).fetchone()
        if row is None:
            hot_runtime_cache_put("scene_graph", cache_key, None)
            request_runtime_cache_put("scene_graph", cache_key, None)
            return None
        payload = _json_loads(row["payload_json"])
        payload.setdefault("graph_kind", normalized_kind)
        payload.setdefault("graph_key", normalized_key)
        payload.setdefault("graph_version", row["graph_version"] or SCENE_GRAPH_VERSION)
        hot_runtime_cache_put("scene_graph", cache_key, payload)
        request_runtime_cache_put("scene_graph", cache_key, payload)
        return payload
    finally:
        connection.close()


def store_scene_graph_record(
    server: Any,
    *,
    graph_kind: str,
    graph_key: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    normalized_kind = str(graph_kind or "").strip()
    normalized_key = _normalize_key(server, normalized_kind, graph_key)
    if not normalized_kind or not normalized_key:
        return dict(payload or {})
    ensure_feature_schema(server)
    stored_payload = dict(payload or {})
    stored_payload["graph_kind"] = normalized_kind
    stored_payload["graph_key"] = normalized_key
    stored_payload.setdefault("graph_version", SCENE_GRAPH_VERSION)
    if db_available():
        try:
            with get_connection() as connection:
                if connection is not None:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            INSERT INTO recommendation_scene_graph_records(
                                graph_kind, graph_key, graph_version, payload, updated_at
                            )
                            VALUES (%s, %s, %s, %s::jsonb, now())
                            ON CONFLICT (graph_kind, graph_key)
                            DO UPDATE SET
                                graph_version = EXCLUDED.graph_version,
                                payload = EXCLUDED.payload,
                                updated_at = now()
                            """,
                            [
                                normalized_kind,
                                normalized_key,
                                str(stored_payload.get("graph_version") or SCENE_GRAPH_VERSION),
                                _json_dumps(stored_payload),
                            ],
                        )
                    pass
        except Exception:
            pass
    connection = open_recommendation_store_connection(server)
    try:
        connection.execute(
            """
            INSERT INTO recommendation_scene_graph_records(
                graph_kind, graph_key, graph_version, payload_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(graph_kind, graph_key)
            DO UPDATE SET
                graph_version = excluded.graph_version,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            [
                normalized_kind,
                normalized_key,
                str(stored_payload.get("graph_version") or SCENE_GRAPH_VERSION),
                _json_dumps(stored_payload),
                float(time.time()),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    cache_key = f"{normalized_kind}:{normalized_key}"
    hot_runtime_cache_put("scene_graph", cache_key, stored_payload)
    request_runtime_cache_put("scene_graph", cache_key, stored_payload)
    return stored_payload


def load_artist_graph(server: Any, artist_key: str) -> Optional[Dict[str, Any]]:
    return load_scene_graph_record(
        server,
        graph_kind="artist",
        graph_key=_normalize_key(server, "artist", artist_key),
    )


def load_scene_cluster_graph(server: Any, scene_cluster_id: str) -> Optional[Dict[str, Any]]:
    return load_scene_graph_record(
        server,
        graph_kind="scene_cluster",
        graph_key=_normalize_key(server, "scene_cluster", scene_cluster_id),
    )


def warm_scene_graph_records(
    server: Any,
    *,
    artist_features: Sequence[Dict[str, Any]],
) -> Dict[str, int]:
    artist_count = 0
    scene_artist_map: Dict[str, set[str]] = defaultdict(set)
    scene_peer_map: Dict[str, set[str]] = defaultdict(set)
    for feature in artist_features or []:
        if not isinstance(feature, dict):
            continue
        artist_key = _normalize_key(server, "artist", feature.get("artist_key"))
        if not artist_key:
            continue
        peer_artist_ids = sorted(
            {
                _normalize_key(server, "artist", item)
                for item in list(feature.get("peer_artist_ids") or [])[:16]
                if _normalize_key(server, "artist", item)
            }
        )
        scene_cluster_ids = sorted(
            {
                _normalize_key(server, "scene_cluster", item)
                for item in list(feature.get("scene_cluster_ids") or [])[:12]
                if _normalize_key(server, "scene_cluster", item)
            }
        )
        store_scene_graph_record(
            server,
            graph_kind="artist",
            graph_key=artist_key,
            payload={
                "artist_key": artist_key,
                "name": str(feature.get("name") or ""),
                "primary_genre": str(feature.get("primary_genre") or ""),
                "subgenre": str(feature.get("subgenre") or ""),
                "language": str(feature.get("language") or ""),
                "region": str(feature.get("region") or ""),
                "scene_cluster_ids": scene_cluster_ids,
                "peer_artist_ids": peer_artist_ids,
                "confidence": float(feature.get("confidence") or 0.0),
                "feature_source": str(feature.get("source_kind") or "stored_enriched"),
                "graph_version": SCENE_GRAPH_VERSION,
            },
        )
        artist_count += 1
        for cluster_id in scene_cluster_ids:
            scene_artist_map[cluster_id].add(artist_key)
            scene_peer_map[cluster_id].update(peer_artist_ids[:8])
    scene_count = 0
    for cluster_id, artist_keys in scene_artist_map.items():
        store_scene_graph_record(
            server,
            graph_kind="scene_cluster",
            graph_key=cluster_id,
            payload={
                "scene_cluster_id": cluster_id,
                "artist_keys": sorted(artist_keys)[:48],
                "peer_artist_ids": sorted(scene_peer_map.get(cluster_id) or set())[:48],
                "graph_version": SCENE_GRAPH_VERSION,
            },
        )
        scene_count += 1
    return {
        "artist_records": artist_count,
        "scene_records": scene_count,
    }


def expand_profile_scene_graph(
    server: Any,
    *,
    artist_keys: Iterable[str],
    scene_cluster_ids: Iterable[str],
) -> Dict[str, Any]:
    peer_artist_keys = set()
    scene_artist_keys = set()
    for artist_key in artist_keys or []:
        record = load_artist_graph(server, _normalize_key(server, "artist", artist_key))
        if not isinstance(record, dict):
            continue
        peer_artist_keys.update(
            _normalize_key(server, "artist", item)
            for item in list(record.get("peer_artist_ids") or [])[:12]
            if _normalize_key(server, "artist", item)
        )
        for cluster_id in list(record.get("scene_cluster_ids") or [])[:8]:
            scene_record = load_scene_cluster_graph(
                server,
                _normalize_key(server, "scene_cluster", cluster_id),
            )
            if not isinstance(scene_record, dict):
                continue
            scene_artist_keys.update(
                _normalize_key(server, "artist", item)
                for item in list(scene_record.get("artist_keys") or [])[:24]
                if _normalize_key(server, "artist", item)
            )
    for cluster_id in scene_cluster_ids or []:
        scene_record = load_scene_cluster_graph(
            server,
            _normalize_key(server, "scene_cluster", cluster_id),
        )
        if not isinstance(scene_record, dict):
            continue
        scene_artist_keys.update(
            _normalize_key(server, "artist", item)
            for item in list(scene_record.get("artist_keys") or [])[:24]
            if _normalize_key(server, "artist", item)
        )
        peer_artist_keys.update(
            _normalize_key(server, "artist", item)
            for item in list(scene_record.get("peer_artist_ids") or [])[:24]
            if _normalize_key(server, "artist", item)
        )
    return {
        "peer_artist_keys": sorted(peer_artist_keys),
        "scene_artist_keys": sorted(scene_artist_keys),
    }
