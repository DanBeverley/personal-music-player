from __future__ import annotations

from typing import Any, Dict

from ..contracts import (
    RecommendationHomeV2Request,
    SearchV2Request,
    SimilarArtistsV2Request,
    SuggestV2Request,
)
from ..legacy import trim_text
from .ranking import model_version
from .user_state import build_home_state, build_search_state, build_similar_artists_state


def build_search_profile(
    req: SearchV2Request | SuggestV2Request,
):
    return build_search_state(req)


def build_home_profile(
    req: RecommendationHomeV2Request,
):
    return build_home_state(req)


def build_similar_artists_profile(
    req: SimilarArtistsV2Request,
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
