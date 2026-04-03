from __future__ import annotations

from threading import Lock
from typing import Any, Dict, Optional
import json
import time

from ..config import get_backend_config

try:
    import redis
except Exception:
    redis = None


class SessionStore:
    def get(self, key: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def set(self, key: str, value: Dict[str, Any], ttl_seconds: int) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


class MemorySessionStore(SessionStore):
    def __init__(self) -> None:
        self._lock = Lock()
        self._items: Dict[str, Dict[str, Any]] = {}

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            payload = self._items.get(key)
            if not payload:
                return None
            if payload["expires_at"] <= now:
                self._items.pop(key, None)
                return None
            return json.loads(json.dumps(payload["value"]))

    def set(self, key: str, value: Dict[str, Any], ttl_seconds: int) -> None:
        with self._lock:
            self._items[key] = {
                "value": json.loads(json.dumps(value)),
                "expires_at": time.time() + ttl_seconds,
            }

    def delete(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)


class RedisSessionStore(SessionStore):
    def __init__(self, url: str) -> None:
        if redis is None:
            raise RuntimeError("redis dependency unavailable")
        self._client = redis.Redis.from_url(url)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        raw = self._client.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def set(self, key: str, value: Dict[str, Any], ttl_seconds: int) -> None:
        self._client.setex(key, ttl_seconds, json.dumps(value, ensure_ascii=False))

    def delete(self, key: str) -> None:
        self._client.delete(key)


_SESSION_STORE: Optional[SessionStore] = None


def get_session_store() -> SessionStore:
    global _SESSION_STORE
    if _SESSION_STORE is not None:
        return _SESSION_STORE
    config = get_backend_config()
    if config.redis_url and redis is not None:
        try:
            _SESSION_STORE = RedisSessionStore(config.redis_url)
            return _SESSION_STORE
        except Exception:
            pass
    _SESSION_STORE = MemorySessionStore()
    return _SESSION_STORE
