from __future__ import annotations

from threading import Lock, Thread
from typing import Any, Dict, Optional
import json
import time
import traceback

from .store_runtime import open_recommendation_store_connection


_MODEL_CACHE = {
    "artifact": None,
    "source_signature": "",
    "expires_at": 0.0,
}
_MODEL_LOCK = Lock()
_MODEL_REFRESH_LOCK = Lock()
_MODEL_REFRESH_THREAD = None
_MODEL_REFRESH_RUNNING = False


def invalidate_model_cache() -> None:
    global _MODEL_REFRESH_THREAD
    global _MODEL_REFRESH_RUNNING
    with _MODEL_LOCK:
        _MODEL_CACHE["artifact"] = None
        _MODEL_CACHE["source_signature"] = ""
        _MODEL_CACHE["expires_at"] = 0.0
    with _MODEL_REFRESH_LOCK:
        _MODEL_REFRESH_RUNNING = False
        _MODEL_REFRESH_THREAD = None


def cache_model_artifact(
    server: Any,
    artifact: Optional[Dict[str, Any]],
    *,
    source_signature: str = "",
) -> None:
    if not isinstance(artifact, dict):
        return
    resolved_signature = (
        server._recommendation_trim_text(source_signature)
        or server._recommendation_trim_text(artifact.get("source_signature"))
    )
    with _MODEL_LOCK:
        _MODEL_CACHE["artifact"] = artifact
        _MODEL_CACHE["source_signature"] = resolved_signature
        _MODEL_CACHE["expires_at"] = (
            time.time() + float(server.RECOMMENDATION_MODEL_CACHE_TTL_SECONDS or 0)
        )


def schedule_model_refresh(server: Any, *, force_sync: bool = False) -> bool:
    if not server.RECOMMENDATION_MODEL_STALE_REFRESH_ENABLED:
        return False
    global _MODEL_REFRESH_THREAD
    global _MODEL_REFRESH_RUNNING
    with _MODEL_REFRESH_LOCK:
        if _MODEL_REFRESH_RUNNING:
            return False
        _MODEL_REFRESH_RUNNING = True

    def _refresh() -> None:
        global _MODEL_REFRESH_RUNNING
        try:
            if force_sync:
                server._recommendation_sync_external_events(force=True)
            latest_signature = server._recommendation_model_source_signature()
            artifact = server._recommendation_train_collaborative_model(latest_signature)
            server._recommendation_store_collaborative_model(artifact)
            cache_model_artifact(
                server,
                artifact,
                source_signature=latest_signature,
            )
        except Exception:
            traceback.print_exc()
        finally:
            with _MODEL_REFRESH_LOCK:
                _MODEL_REFRESH_RUNNING = False

    thread = Thread(
        target=_refresh,
        name="recommendation-model-refresh",
        daemon=True,
    )
    _MODEL_REFRESH_THREAD = thread
    thread.start()
    return True


def get_collaborative_model(
    server: Any,
    *,
    force_refresh: bool = False,
    force_sync: bool = False,
):
    if force_sync:
        server._recommendation_sync_external_events(force=True)
    source_signature = server._recommendation_model_source_signature()
    now = time.time()
    with _MODEL_LOCK:
        cached_artifact = _MODEL_CACHE.get("artifact")
        cached_signature = _MODEL_CACHE.get("source_signature") or ""
        cached_expires_at = float(_MODEL_CACHE.get("expires_at") or 0.0)
        if (
            not force_refresh
            and cached_artifact is not None
            and cached_signature == source_signature
            and cached_expires_at > now
        ):
            return cached_artifact
    if not force_refresh and isinstance(cached_artifact, dict):
        if cached_signature and cached_signature != source_signature:
            schedule_model_refresh(server, force_sync=False)
        if cached_expires_at > now:
            return cached_artifact

    connection = open_recommendation_store_connection(server)
    try:
        row = connection.execute(
            """
            SELECT source_signature, artifact_json
            FROM recommendation_models
            WHERE id = ?
            """,
            ["global"],
        ).fetchone()
    finally:
        connection.close()

    artifact = None
    row_signature = ""
    if row is not None:
        row_signature = server._recommendation_trim_text(row["source_signature"] or "")
        try:
            artifact = json.loads(row["artifact_json"] or "{}")
        except Exception:
            artifact = None
    if not force_refresh and isinstance(artifact, dict):
        cache_model_artifact(
            server,
            artifact,
            source_signature=row_signature or source_signature,
        )
        if row_signature and row_signature != source_signature:
            schedule_model_refresh(server, force_sync=False)
        return artifact
    if not force_refresh and isinstance(cached_artifact, dict):
        if cached_signature and cached_signature != source_signature:
            schedule_model_refresh(server, force_sync=False)
        return cached_artifact
    if not force_refresh:
        schedule_model_refresh(server, force_sync=False)
        return {
            "ready": False,
            "reason": "model_warming_up",
            "event_count": 0,
            "trained_at": time.time(),
            "source_signature": source_signature,
        }

    artifact = server._recommendation_train_collaborative_model(source_signature)
    server._recommendation_store_collaborative_model(artifact)
    cache_model_artifact(
        server,
        artifact,
        source_signature=source_signature,
    )
    return artifact
