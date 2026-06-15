from __future__ import annotations

__all__ = ["RecommendationService"]


def __getattr__(name: str):
    if name == "RecommendationService":
        from .service import RecommendationService

        return RecommendationService
    raise AttributeError(name)
