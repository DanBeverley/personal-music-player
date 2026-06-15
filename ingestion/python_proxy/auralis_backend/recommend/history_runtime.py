from __future__ import annotations

from typing import Any, Dict

from ..domain.server_adapter import adapt_domain_server
from ..domain.user_state import _load_scope_history_seed


def history_seed(server: Any, req: Any) -> Dict[str, Any]:
    normalized_scope = server._assistant_safe_scope_id(
        getattr(req, "user_scope_id", "guest") or "guest"
    )
    limit = max(1, min(int(getattr(req, "limit", 24) or 24), 50))
    diagnostics: Dict[str, Any] = {}
    try:
        seed = _load_scope_history_seed(
            adapt_domain_server(server),
            normalized_scope,
        )
    except Exception as exc:
        seed = {}
        diagnostics["history_seed_error"] = str(exc)[:240]
        print(
            "[EBB:history_seed][error] "
            f"scope={normalized_scope} error={str(exc)[:240]}",
            flush=True,
        )
    recent_tracks = list(seed.get("recent_track_snapshots") or [])[:limit]
    top_tracks = list(seed.get("top_track_snapshots") or [])[:limit]
    last_played = list(seed.get("last_played_tracks") or [])[:limit]
    diagnostics.update(dict(seed.get("_diagnostics") or {}))
    return {
        "status": "success",
        "user_scope_id": normalized_scope,
        "recent_track_snapshots": recent_tracks,
        "top_track_snapshots": top_tracks,
        "last_played_tracks": last_played,
        "recent_track_ids": list(seed.get("recent_track_ids") or [])[:limit],
        "top_track_ids": list(seed.get("top_track_ids") or [])[:limit],
        "diagnostics": diagnostics,
    }
