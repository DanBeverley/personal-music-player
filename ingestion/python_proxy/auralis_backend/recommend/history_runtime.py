from __future__ import annotations

from typing import Any, Dict

from ..domain.server_adapter import adapt_domain_server
from ..domain.user_state import _build_scope_history_seed_from_events


def history_seed(server: Any, req: Any) -> Dict[str, Any]:
    normalized_scope = server._assistant_safe_scope_id(
        getattr(req, "user_scope_id", "guest") or "guest"
    )
    limit = max(1, min(int(getattr(req, "limit", 24) or 24), 50))
    diagnostics: Dict[str, Any] = {}
    try:
        # This display endpoint reads committed account events. The profile's
        # process-local snapshot may lag another device or the durable worker.
        # No remote metadata acquisition or snapshot writes on this path.
        seed = _build_scope_history_seed_from_events(
            adapt_domain_server(server),
            normalized_scope,
            pending_play_keys=frozenset(
                str(value) for value in (getattr(req, "pending_play_keys", []) or [])[:512]
            ),
            strict_errors=True,
        )
    except Exception as exc:
        # An unavailable store is not an empty account. Clients retain their
        # last confirmed history and retry without replacing it with emptiness.
        return {"status": "error", "user_scope_id": normalized_scope,
                "diagnostics": {"history_seed_error": type(exc).__name__}}
    recent_tracks = [item for item in (seed.get("history_track_snapshots") or [])
                     if int(item.get("play_count") or 0) > 0][:limit]
    top_tracks = list(seed.get("top_track_snapshots") or [])[:limit]
    last_played = recent_tracks[:limit]
    frequent_tracks = list(seed.get("frequent_track_snapshots") or [])[:limit]
    diagnostics.update(dict(seed.get("_diagnostics") or {}))
    return {
        "status": "success",
        "user_scope_id": normalized_scope,
        "recent_track_snapshots": recent_tracks,
        "top_track_snapshots": top_tracks,
        "last_played_tracks": last_played,
        "frequent_track_snapshots": frequent_tracks,
        "acknowledged_play_keys": list(seed.get("acknowledged_play_keys") or []),
        "recent_track_ids": list(seed.get("recent_track_ids") or [])[:limit],
        "top_track_ids": list(seed.get("top_track_ids") or [])[:limit],
        "diagnostics": diagnostics,
    }
