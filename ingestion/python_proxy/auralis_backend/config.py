from __future__ import annotations

from dataclasses import dataclass
import os


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BackendConfig:
    postgres_dsn: str
    redis_url: str
    gcs_bucket: str
    session_ttl_seconds: int
    request_log_ttl_seconds: int
    model_namespace: str
    enable_pgvector: bool


def get_backend_config() -> BackendConfig:
    postgres_dsn = (
        os.environ.get("AURALIS_PGVECTOR_DSN")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("RECOMMENDATION_SYNC_DATABASE_DSN")
        or ""
    ).strip()
    return BackendConfig(
        postgres_dsn=postgres_dsn,
        redis_url=(os.environ.get("REDIS_URL") or "").strip(),
        gcs_bucket=(os.environ.get("GCS_BUCKET") or "").strip(),
        session_ttl_seconds=int(os.environ.get("AURALIS_SESSION_TTL_SECONDS", "1800")),
        request_log_ttl_seconds=int(
            os.environ.get("AURALIS_REQUEST_LOG_TTL_SECONDS", "86400")
        ),
        model_namespace=(os.environ.get("AURALIS_MODEL_NAMESPACE") or "auralis-v2").strip(),
        enable_pgvector=_env_bool("AURALIS_ENABLE_PGVECTOR", True),
    )
