from __future__ import annotations

from typing import Any

from ..legacy import get_server


def resolve_server(server: Any | None = None) -> Any:
    return server or get_server()


def init_recommendation_store(server: Any | None = None) -> Any:
    resolved = resolve_server(server)
    resolved._recommendation_init_store_db()
    return resolved


def open_recommendation_store_connection(server: Any | None = None):
    resolved = init_recommendation_store(server)
    return resolved._recommendation_store_connection()
