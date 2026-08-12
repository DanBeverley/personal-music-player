from __future__ import annotations

import os
import json
import time
from typing import Any

from ..storage.cache_runtime import (
    lookup_ttl_cache,
    search_result_cache,
    search_result_cache_lock,
    store_ttl_cache,
)
from ..recommend.store_runtime import open_recommendation_store_connection


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


def lookup_persistent_suggestion_base(server: Any, key: str) -> Any:
    """Best-effort shared cache surviving process restarts."""
    try:
        connection = open_recommendation_store_connection(server)
        row = connection.execute("SELECT payload_json FROM search_suggestion_cache WHERE cache_key = ?", (key,)).fetchone()
        if not row:
            return None
        connection.execute("UPDATE search_suggestion_cache SET updated_at = ? WHERE cache_key = ?", (time.time(), key))
        connection.commit()
        return json.loads(row[0])
    except Exception:
        return None
    finally:
        try:
            connection.close()
        except Exception:
            pass


def store_persistent_suggestion_base(server: Any, key: str, value: Any, *, max_entries: int = 256) -> None:
    try:
        connection = open_recommendation_store_connection(server)
        connection.execute("INSERT OR REPLACE INTO search_suggestion_cache(cache_key,payload_json,updated_at) VALUES(?,?,?)", (key, json.dumps(value, ensure_ascii=False), time.time()))
        count = int(connection.execute("SELECT COUNT(*) FROM search_suggestion_cache").fetchone()[0])
        excess = max(0, count - max_entries)
        if excess:
            connection.execute("DELETE FROM search_suggestion_cache WHERE cache_key IN (SELECT cache_key FROM search_suggestion_cache ORDER BY updated_at ASC LIMIT ?)", (excess,))
        connection.commit()
    except Exception:
        return None
    finally:
        try:
            connection.close()
        except Exception:
            pass
