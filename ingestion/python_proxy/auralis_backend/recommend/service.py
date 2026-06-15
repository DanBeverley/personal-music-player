from __future__ import annotations

from typing import Any, Dict
import time
import traceback
import uuid

from fastapi import HTTPException

from ..discovery.service import DiscoveryService
from .admin_runtime import (
    evaluate_experiments as evaluate_experiments_runtime,
    experiments as experiments_runtime,
    interaction_event as interaction_event_runtime,
    model_status as model_status_runtime,
    model_versions as model_versions_runtime,
    recommended_artists as recommended_artists_runtime,
    search_interaction as search_interaction_runtime,
    train_model as train_model_runtime,
)
from .feature_store import request_store_runtime
from .history_runtime import history_seed as history_seed_runtime

try:
    from ..storage.postgres import (
        activate_model_version,
        list_model_versions,
        list_rollout_events,
        rollback_model_version,
    )
except Exception:
    def list_model_versions(*, model_key: str, limit: int = 20):
        return []

    def activate_model_version(
        *,
        model_key: str,
        version: str,
        actor: str = "system",
        reason: str = "",
        metadata: Dict[str, Any] | None = None,
    ):
        return False

    def rollback_model_version(
        *,
        model_key: str,
        target_version: str = "",
        actor: str = "system",
        reason: str = "",
        metadata: Dict[str, Any] | None = None,
    ):
        return {"ok": False, "reason": "postgres_unavailable"}

    def list_rollout_events(*, model_key: str = "", limit: int = 50):
        return []


class RecommendationService:
    """Compatibility facade for Discovery Engine and recommendation admin APIs."""

    def __init__(self, server: Any) -> None:
        self._server = server
        self._discovery = DiscoveryService(server)

    def recommend(self, req: Any) -> Dict[str, Any]:
        server = self._server
        trace = server._trace_start(
            "recommend",
            user_scope_id=req.user_scope_id or "guest",
            surface=req.surface or "home_feed",
            query=req.query or "",
        )
        request_mode = "unknown"
        started_at = time.perf_counter()
        try:
            parse_started_at = time.perf_counter()
            is_row_page = bool(
                server._recommendation_trim_text(getattr(req, "session_id", ""))
                and server._recommendation_trim_text(getattr(req, "row_id", ""))
            )
            if is_row_page:
                request_mode = "row_page"
            elif bool(getattr(req, "prepare_next_session", False)):
                request_mode = "background_prepare"
            else:
                request_mode = "full_feed"
            server._trace_put(
                trace,
                "ranking_meta",
                "recommend.request_mode",
                request_mode,
            )
            server._trace_put(
                trace,
                "ranking_meta",
                "recommend.impl",
                "discovery_engine",
            )
            server._trace_stage(trace, "recommend.request_parse", parse_started_at)
            print(
                "[EBB:recommend][progress] "
                f"request_id={trace.get('request_id') or ''} "
                f"stage=request_parse mode={request_mode} impl=discovery_engine",
                flush=True,
            )

            with request_store_runtime(allow_persistent_reads=False):
                response = self._discovery.recommend(
                    req,
                    request_mode=request_mode,
                    trace=trace,
                )

            serialize_started_at = time.perf_counter()
            server._trace_stage(trace, "recommend.serialize", serialize_started_at)
            diagnostics = response.get("diagnostics")
            if isinstance(diagnostics, dict):
                diagnostics.setdefault(
                    "request_ms",
                    int((time.perf_counter() - started_at) * 1000),
                )
                diagnostics.update(
                    server._trace_diagnostics(
                        server._trace_finalize(trace, status="success"),
                    )
                )
                response["request_id"] = (
                    diagnostics.get("request_id")
                    or trace.get("request_id")
                    or str(uuid.uuid4())
                )
            else:
                server._trace_finalize(trace, status="success")

            server._trace_log_request(
                trace,
                request_type="recommend",
                user_scope_id=req.user_scope_id or "guest",
                session_id=server._recommendation_trim_text(
                    response.get("session_id"),
                ),
                model_version=server._recommendation_trim_text(
                    (response.get("diagnostics") or {}).get("model_version"),
                ),
            )
            return response
        except HTTPException:
            server._trace_finalize(trace, status="failed")
            server._trace_log_request(
                trace,
                request_type="recommend",
                user_scope_id=req.user_scope_id or "guest",
            )
            raise
        except Exception as exc:
            print(
                "[EBB:recommend][error] "
                f"mode={request_mode} "
                f"scope={server._recommendation_trim_text(req.user_scope_id or 'guest')} "
                f"session_id={server._recommendation_trim_text(getattr(req, 'session_id', ''))} "
                f"row_id={server._recommendation_trim_text(getattr(req, 'row_id', ''))} "
                f"error={str(exc)[:240]}",
                flush=True,
            )
            print(traceback.format_exc(), flush=True)
            server._trace_finalize(trace, status="failed", error=str(exc))
            server._trace_log_request(
                trace,
                request_type="recommend",
                user_scope_id=req.user_scope_id or "guest",
            )
            raise HTTPException(status_code=500, detail=str(exc))

    def recommended_artists(self, req: Any):
        return recommended_artists_runtime(self._server, req)

    def interaction_event(self, req: Any):
        return interaction_event_runtime(self._server, req)

    def search_interaction(self, req: Any):
        return search_interaction_runtime(self._server, req)

    def history_seed(self, req: Any):
        return history_seed_runtime(self._server, req)

    def model_status(self):
        return model_status_runtime(self._server)

    def model_versions(self):
        return model_versions_runtime(self._server)

    def experiments(self, *, window_hours: int):
        return experiments_runtime(self._server, window_hours=window_hours)

    def evaluate_experiments(self, *, force_promote: bool, window_hours: int):
        return evaluate_experiments_runtime(
            self._server,
            force_promote=force_promote,
            window_hours=window_hours,
        )

    def train_model(self, req: Any):
        return train_model_runtime(self._server, req)

    def model_registry_versions(self, *, model_key: str, limit: int = 20):
        normalized_key = self._server._recommendation_trim_text(model_key)
        if not normalized_key:
            raise HTTPException(status_code=400, detail="model_key is required")
        return {
            "status": "success",
            "model_key": normalized_key,
            "versions": list_model_versions(
                model_key=normalized_key,
                limit=limit,
            ),
        }

    def model_registry_activate(
        self,
        *,
        model_key: str,
        version: str,
        actor: str = "system",
        reason: str = "",
    ):
        normalized_key = self._server._recommendation_trim_text(model_key)
        normalized_version = self._server._recommendation_trim_text(version)
        if not normalized_key or not normalized_version:
            raise HTTPException(
                status_code=400,
                detail="model_key and version are required",
            )
        ok = activate_model_version(
            model_key=normalized_key,
            version=normalized_version,
            actor=self._server._recommendation_trim_text(actor) or "system",
            reason=self._server._recommendation_trim_text(reason),
            metadata={"source": "api"},
        )
        if not ok:
            raise HTTPException(status_code=404, detail="model version not found")
        return {
            "status": "success",
            "model_key": normalized_key,
            "active_version": normalized_version,
        }

    def model_registry_rollback(
        self,
        *,
        model_key: str,
        target_version: str = "",
        actor: str = "system",
        reason: str = "",
    ):
        normalized_key = self._server._recommendation_trim_text(model_key)
        if not normalized_key:
            raise HTTPException(status_code=400, detail="model_key is required")
        result = rollback_model_version(
            model_key=normalized_key,
            target_version=self._server._recommendation_trim_text(target_version),
            actor=self._server._recommendation_trim_text(actor) or "system",
            reason=self._server._recommendation_trim_text(reason),
            metadata={"source": "api"},
        )
        if not bool((result or {}).get("ok")):
            raise HTTPException(
                status_code=400,
                detail=f"rollback failed: {(result or {}).get('reason') or 'unknown'}",
            )
        return {
            "status": "success",
            "model_key": normalized_key,
            "rollback": result,
        }

    def model_rollout_events(self, *, model_key: str = "", limit: int = 50):
        normalized_key = self._server._recommendation_trim_text(model_key)
        return {
            "status": "success",
            "model_key": normalized_key,
            "events": list_rollout_events(
                model_key=normalized_key,
                limit=limit,
            ),
        }
