from __future__ import annotations

from typing import Any, Dict

from ..contracts import SearchRequest
from .server_adapter import adapt_domain_server
from .ranking import model_version
from .user_state import build_home_state, build_similar_artists_state


def trim_text(value: str | None) -> str:
    return adapt_domain_server().trim_text(value)


def build_home_profile(
    req: SearchRequest,
):
    return build_home_state(req)


def build_similar_artists_profile(
    req: SearchRequest,
):
    return build_similar_artists_state(req)


def build_recommendation_model_version(
    *,
    prefix: str,
    profile: Dict[str, Any],
) -> str:
    collaborative = profile.get("collaborative") or {}
    model_id = trim_text(collaborative.get("model_id")) or "fallback"
    return f"{model_version(prefix)}:{model_id}"
