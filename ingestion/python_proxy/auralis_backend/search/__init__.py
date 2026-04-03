from __future__ import annotations

__all__ = ["SearchService"]


def __getattr__(name: str):
    if name == "SearchService":
        from .service import SearchService

        return SearchService
    raise AttributeError(name)
