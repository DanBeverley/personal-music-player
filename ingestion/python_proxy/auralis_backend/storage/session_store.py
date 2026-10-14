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
    def __init__(self, *, max_bytes: int = 64 * 1024 * 1024, max_entries: int = 256) -> None:
        self._lock = Lock()
        self._items: Dict[str, Dict[str, Any]] = {}
        self._max_bytes = max(0, int(max_bytes))
        self._max_entries = max(0, int(max_entries))
        self._size_bytes = 0

    def _remove(self, key: str) -> None:
        previous = self._items.pop(key, None)
        if previous is not None:
            self._size_bytes -= len(previous["value"])

    def _expire(self, now: float) -> None:
        for key in list(self._items):
            if self._items[key]["expires_at"] <= now:
                self._remove(key)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            self._expire(now)
            payload = self._items.get(key)
            if not payload:
                return None
            # Retain serialized bytes, not a second expanded inventory tree.
            raw = payload["value"]
        return json.loads(raw)

    def set(self, key: str, value: Dict[str, Any], ttl_seconds: int) -> None:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self._lock:
            self._expire(time.time())
            self._remove(key)
            if len(raw) > self._max_bytes or ttl_seconds <= 0 or not self._max_entries:
                return
            self._items[key] = {
                "value": raw,
                "expires_at": time.time() + ttl_seconds,
            }
            self._size_bytes += len(raw)
            while self._items and (self._size_bytes > self._max_bytes or len(self._items) > self._max_entries):
                self._remove(next(iter(self._items)))

    def delete(self, key: str) -> None:
        with self._lock:
            self._remove(key)


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
