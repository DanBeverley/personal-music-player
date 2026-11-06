from __future__ import annotations

from typing import Any
import json

from fastapi import HTTPException

from .store_runtime import open_recommendation_store_connection


def recommended_artists(server: Any, req: Any):
    try:
        return server._recommended_artists_payload(req)
    except Exception:
        return {"status": "success", "artists": []}


def interaction_event(server: Any, req: Any):
    try:
        stored = server._recommendation_store_interaction_event(req)
        return {"status": "success", "stored": bool(stored)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def search_interaction(server: Any, req: Any):
    try:
        stored = server._recommendation_store_search_event(req)
        return {"status": "success", "stored": bool(stored)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def recent_search_picks(server: Any, *, user_scope_id: str, limit: int = 8):
    scope = str(user_scope_id or "guest").strip() or "guest"
    resolved_limit = max(1, min(int(limit or 8), 24))
    try:
        connection = open_recommendation_store_connection(server)
        try:
            rows = connection.execute(
                """
                SELECT query, metadata_json, occurred_at
                FROM recommendation_search_events
                WHERE user_scope_id = ?
                  AND (
                    metadata_json LIKE '%"selected_item"%'
                    OR metadata_json LIKE '%"clicked_item"%'
                    OR metadata_json LIKE '%"track"%'
                    OR metadata_json LIKE '%"entity"%'
                  )
                ORDER BY occurred_at DESC
                LIMIT ?
                """,
                [scope, resolved_limit * 30],
            ).fetchall()
        finally:
            connection.close()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    picks = []
    seen = set()
    for row in rows:
        try:
            payload = json.loads(row["metadata_json"] or "{}")
        except Exception:
            payload = {}
        selected = {}
        for key in ("selected_item", "clicked_item", "track", "item", "entity"):
            value = payload.get(key)
            if isinstance(value, dict):
                selected = dict(value)
                break
        entity_type = str(
            payload.get("selected_entity_type")
            or payload.get("clicked_entity_type")
            or payload.get("entity_type")
            or ("track" if selected else "")
            or ""
        ).strip().lower()
        if entity_type and entity_type != "track":
            continue
        if not selected:
            continue
        track_id = str(
            selected.get("id")
            or selected.get("videoId")
            or selected.get("track_id")
            or selected.get("video_id")
            or ""
        ).strip()
        title = str(selected.get("title") or selected.get("name") or "").strip()
        artist = str(
            selected.get("artist")
            or selected.get("channel")
            or selected.get("author")
            or ""
        ).strip()
        key = track_id or f"{title.lower()}|{artist.lower()}"
        if not key or key in seen:
            continue
        seen.add(key)
        picks.append(
            {
                "query": str(row["query"] or "").strip(),
                "track": selected,
                "selected_at": float(row["occurred_at"] or 0.0),
            }
        )
        if len(picks) >= resolved_limit:
            break
    return {"status": "success", "picks": picks}


def model_status(server: Any):
    try:
        model = server._recommendation_get_collaborative_model()
        sync_signature = server._recommendation_model_source_signature()
        try:
            sync_payload = server.json.loads(sync_signature)
        except Exception:
            sync_payload = {}
        return {
            "status": "success",
            "model": {
                "ready": bool((model or {}).get("ready")),
                "model_id": (model or {}).get("model_id") or "",
                "model_type": (model or {}).get("model_type") or "",
                "event_count": int((model or {}).get("event_count") or 0),
                "search_event_count": int((model or {}).get("search_event_count") or 0),
                "user_count": int((model or {}).get("user_count") or 0),
                "item_count": int((model or {}).get("item_count") or 0),
                "factor_dim": int((model or {}).get("factor_dim") or 0),
                "trained_at": (model or {}).get("trained_at"),
                "source_signature": (model or {}).get("source_signature") or "",
                "evaluation_metrics": (model or {}).get("evaluation_metrics") or {},
                "sync_state": {
                    "dsn_configured": bool(server.RECOMMENDATION_SYNC_DATABASE_DSN),
                    "scheduler_enabled": server.RECOMMENDATION_ENABLE_SCHEDULER,
                    "event_count": int(sync_payload.get("event_count") or 0),
                    "search_event_count": int(sync_payload.get("search_event_count") or 0),
                    "user_count": int(sync_payload.get("user_count") or 0),
                    "item_count": int(sync_payload.get("item_count") or 0),
                    "external_sync": server._recommendation_external_sync_health_snapshot(),
                },
                "runtime": server._recommendation_runtime_snapshot(),
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def model_versions(server: Any):
    try:
        tracked_model_keys = [
            "home_global_ranker_v4",
            "home_continue_ranker_v1",
            "home_because_played_ranker_v1",
            "home_quiet_ranker_v1",
            "home_trending_ranker_v1",
            "home_discovery_ranker_v1",
        ]
        return {
            "status": "success",
            "runtime": server._recommendation_runtime_snapshot(version_limit=12),
            "model_registry": {
                model_key: server._pg_list_model_versions(model_key=model_key, limit=4)
                for model_key in tracked_model_keys
            },
            "rollout_events": server._pg_list_rollout_events(limit=24),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def experiments(server: Any, *, window_hours: int):
    try:
        return {
            "status": "success",
            "experiments": server._recommendation_experiment_dashboard(
                window_hours=window_hours,
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def evaluate_experiments(server: Any, *, force_promote: bool, window_hours: int):
    try:
        return {
            "status": "success",
            "result": server._recommendation_evaluate_experiment(
                force_promote=force_promote,
                window_hours=window_hours,
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def train_model(server: Any, req: Any):
    try:
        if req.force_sync:
            server._recommendation_sync_external_events(force=True)
        model = server._recommendation_get_collaborative_model(force_refresh=True)
        return {
            "status": "success",
            "model": {
                "ready": bool((model or {}).get("ready")),
                "model_id": (model or {}).get("model_id") or "",
                "model_type": (model or {}).get("model_type") or "",
                "event_count": int((model or {}).get("event_count") or 0),
                "search_event_count": int((model or {}).get("search_event_count") or 0),
                "user_count": int((model or {}).get("user_count") or 0),
                "item_count": int((model or {}).get("item_count") or 0),
                "factor_dim": int((model or {}).get("factor_dim") or 0),
                "trained_at": (model or {}).get("trained_at"),
                "source_signature": (model or {}).get("source_signature") or "",
                "evaluation_metrics": (model or {}).get("evaluation_metrics") or {},
                "runtime": server._recommendation_runtime_snapshot(),
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
