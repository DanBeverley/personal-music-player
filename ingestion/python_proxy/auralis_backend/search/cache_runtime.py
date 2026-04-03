from __future__ import annotations

import os
from typing import Any

from ..storage.cache_runtime import (
    lookup_ttl_cache,
    search_result_cache,
    search_result_cache_lock,
    store_ttl_cache,
)


SEARCH_RESULT_CACHE_TTL_SECONDS = int(os.environ.get("SEARCH_RESULT_CACHE_TTL_SECONDS", "600"))


def lookup_search_result(namespace: str, key: str) -> Any:
    return lookup_ttl_cache(
        search_result_cache,
        search_result_cache_lock,
        namespace,
        key,
    )


def store_search_result(
    namespace: str,
    key: str,
    value: Any,
    *,
    ttl_seconds: int | None = None,
) -> None:
    store_ttl_cache(
        search_result_cache,
        search_result_cache_lock,
        namespace,
        key,
        value,
        ttl_seconds if ttl_seconds is not None else SEARCH_RESULT_CACHE_TTL_SECONDS,
    )
