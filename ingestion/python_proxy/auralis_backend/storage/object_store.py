from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
import json
import time

from ..config import get_backend_config

try:
    from google.cloud import storage as gcs_storage
except Exception:
    gcs_storage = None


class ObjectStore:
    def write_json(self, name: str, payload: Dict[str, Any]) -> str:
        raise NotImplementedError


class LocalObjectStore(ObjectStore):
    def __init__(self) -> None:
        self._root = Path.cwd() / "runtime" / "auralis_exports"
        self._root.mkdir(parents=True, exist_ok=True)

    def write_json(self, name: str, payload: Dict[str, Any]) -> str:
        path = self._root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)


class GCSObjectStore(ObjectStore):
    def __init__(self, bucket_name: str) -> None:
        if gcs_storage is None:
            raise RuntimeError("google-cloud-storage dependency unavailable")
        self._client = gcs_storage.Client()
        self._bucket = self._client.bucket(bucket_name)

    def write_json(self, name: str, payload: Dict[str, Any]) -> str:
        blob = self._bucket.blob(name)
        blob.upload_from_string(
            json.dumps(payload, ensure_ascii=False),
            content_type="application/json",
        )
        return f"gs://{self._bucket.name}/{name}"


_OBJECT_STORE: ObjectStore | None = None


def get_object_store() -> ObjectStore:
    global _OBJECT_STORE
    if _OBJECT_STORE is not None:
        return _OBJECT_STORE
    config = get_backend_config()
    if config.gcs_bucket and gcs_storage is not None:
        try:
            _OBJECT_STORE = GCSObjectStore(config.gcs_bucket)
            return _OBJECT_STORE
        except Exception:
            pass
    _OBJECT_STORE = LocalObjectStore()
    return _OBJECT_STORE
