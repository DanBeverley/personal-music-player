from __future__ import annotations

import time
import traceback
from typing import Any

from fastapi import HTTPException

from ..assistant_core_runtime import (
    assistant_create_session,
    assistant_delete_session,
    assistant_get_session,
    assistant_get_session_detail,
    assistant_list_sessions,
    assistant_safe_scope_id,
    assistant_store_session_message,
    assistant_touch_session,
    assistant_update_session,
)
from ..assistant_tool_runtime import (
    assistant_fallback_chat_reply,
    assistant_langgraph_deps,
    assistant_model_for_request,
    assistant_store_turn_memory,
)


class AssistantService:
    def __init__(self, server: Any) -> None:
        self._server = server

    def list_sessions(
        self,
        user_scope_id: str,
        *,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        sessions = assistant_list_sessions(
            self._server,
            user_scope_id,
            include_archived=include_archived,
        )
        return {"status": "success", "sessions": sessions}

    def create_session(self, req: Any) -> dict[str, Any]:
        session = assistant_create_session(
            self._server,
            req.user_scope_id,
            title=req.title,
        )
        return {"status": "success", "session": session}

    def get_session(self, session_id: str, user_scope_id: str) -> dict[str, Any]:
        detail = assistant_get_session_detail(self._server, session_id, user_scope_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Assistant session not found")
        return {"status": "success", **detail}

    def update_session(self, session_id: str, req: Any) -> dict[str, Any]:
        session = assistant_update_session(
            self._server,
            session_id,
            req.user_scope_id,
            title=req.title,
            archived=req.archived,
            pinned=req.pinned,
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Assistant session not found")
        return {"status": "success", "session": session}

    def delete_session(self, session_id: str, user_scope_id: str) -> dict[str, Any]:
        deleted = assistant_delete_session(self._server, session_id, user_scope_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Assistant session not found")
        return {"status": "success"}

    def chat(self, req: Any) -> dict[str, Any]:
        server = self._server
        session = None
        scope_id = assistant_safe_scope_id(server, req.user_scope_id)
        try:
            request_started_at = time.perf_counter()
            if req.session_id:
                session = assistant_get_session(server, req.session_id, scope_id)
            if session is None:
                session = assistant_create_session(
                    server,
                    scope_id,
                    seed_message=req.message,
                )
                req.session_id = session["id"]

            assistant_store_session_message(
                server,
                session["id"],
                scope_id,
                role="user",
                content=req.message,
                payload={"role": "user"},
            )
            assistant_touch_session(
                server,
                session["id"],
                scope_id,
                last_message_preview=req.message,
            )

            selected_model = assistant_model_for_request(server, req)
            run_langgraph = getattr(server, "run_langgraph_assistant", None)
            langgraph_runtime_available = getattr(
                server,
                "langgraph_runtime_available",
                lambda: False,
            )
            if (
                bool(getattr(server, "USE_LANGGRAPH_ASSISTANT", False))
                and callable(langgraph_runtime_available)
                and langgraph_runtime_available()
                and callable(run_langgraph)
            ):
                payload = run_langgraph(req, assistant_langgraph_deps(server, req))
            else:
                reply = assistant_fallback_chat_reply(
                    server,
                    req,
                    model_override=selected_model,
                )
                assistant_store_turn_memory(
                    server,
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

            assistant_store_session_message(
                server,
                session["id"],
                scope_id,
                role="assistant",
                content=payload.get("reply") or "",
                payload=payload,
            )
            assistant_touch_session(
                server,
                session["id"],
                scope_id,
                last_message_preview=payload.get("reply") or req.message,
                last_mode=payload.get("mode"),
            )
            refreshed_session = (
                assistant_get_session(server, session["id"], scope_id) or session
            )
            return {
                **payload,
                "session_id": refreshed_session["id"],
                "session_title": refreshed_session["title"],
                "session": refreshed_session,
            }
        except Exception as exc:
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
                        "error": str(exc),
                        "total_http_ms": 0,
                    },
                }
                assistant_store_session_message(
                    server,
                    session["id"],
                    scope_id,
                    role="assistant",
                    content=payload["reply"],
                    payload=payload,
                )
                assistant_touch_session(
                    server,
                    session["id"],
                    scope_id,
                    last_message_preview=payload["reply"],
                    last_mode=payload["mode"],
                )
                refreshed_session = (
                    assistant_get_session(server, session["id"], scope_id) or session
                )
                return {
                    **payload,
                    "session_id": refreshed_session["id"],
                    "session_title": refreshed_session["title"],
                    "session": refreshed_session,
                }
            raise HTTPException(status_code=500, detail=str(exc))
