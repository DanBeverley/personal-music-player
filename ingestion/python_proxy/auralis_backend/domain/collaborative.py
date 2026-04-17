from __future__ import annotations

from typing import Any, Dict

from .server_adapter import adapt_domain_server


def track_scores(server: Any, track, profile: Dict[str, Any]) -> Dict[str, float]:
    server = adapt_domain_server(server)
    if not isinstance(track, dict):
        return {
            "latent": 0.0,
            "neighbor": 0.0,
            "artist": 0.0,
        }

    collaborative = profile.get("collaborative") or {}
    track_id = server.trim_text(track.get("id"))
    artist_key = server.normalize_text(
        track.get("channel") or track.get("author") or track.get("artist") or ""
    )
    neighbor_score = float((collaborative.get("neighbor_scores") or {}).get(track_id) or 0.0)
    artist_score = float((collaborative.get("artist_scores") or {}).get(artist_key) or 0.0)
    latent_score = 0.0
    user_vector = collaborative.get("user_vector") or []
    if track_id and user_vector:
        model = collaborative.get("model") or {}
        if isinstance(model, dict) and model.get("ready"):
            item_vector = (model.get("item_factors") or {}).get(track_id) or []
            latent_score = max(0.0, server.cosine_similarity(user_vector, item_vector))
    return {
        "latent": latent_score,
        "neighbor": neighbor_score,
        "artist": artist_score,
    }
