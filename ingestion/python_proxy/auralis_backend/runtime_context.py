from __future__ import annotations

from typing import Any


def get_server() -> Any:
    import server as server_module

    return server_module


def resolve_server(server: Any | None = None) -> Any:
    return server or get_server()
