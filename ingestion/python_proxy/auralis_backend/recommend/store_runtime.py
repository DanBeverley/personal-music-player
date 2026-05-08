from __future__ import annotations

from typing import Any
import sqlite3

from ..runtime_context import resolve_server as _resolve_runtime_server


def resolve_server(server: Any | None = None) -> Any:
    return _resolve_runtime_server(server)


def init_recommendation_store(server: Any | None = None) -> Any:
    resolved = resolve_server(server)
    with resolved.recommendation_store_lock:
        connection = open_recommendation_store_connection_raw(resolved)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_events (
                    id TEXT PRIMARY KEY,
                    user_scope_id TEXT NOT NULL,
                    track_id TEXT NOT NULL,
                    artist_name TEXT,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    weight REAL NOT NULL,
                    metadata_json TEXT,
                    occurred_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_recommendation_events_user_time "
                "ON recommendation_events(user_scope_id, occurred_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_recommendation_events_track_time "
                "ON recommendation_events(track_id, occurred_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_models (
                    id TEXT PRIMARY KEY,
                    source_signature TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            existing_model_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(recommendation_models)"
                ).fetchall()
            }
            if "metrics_json" not in existing_model_columns:
                connection.execute(
                    "ALTER TABLE recommendation_models ADD COLUMN metrics_json TEXT"
                )
            if "is_active" not in existing_model_columns:
                connection.execute(
                    "ALTER TABLE recommendation_models ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0"
                )
            if "created_at" not in existing_model_columns:
                connection.execute(
                    "ALTER TABLE recommendation_models ADD COLUMN created_at REAL"
                )
            if "model_kind" not in existing_model_columns:
                connection.execute(
                    "ALTER TABLE recommendation_models ADD COLUMN model_kind TEXT"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_model_versions (
                    id TEXT PRIMARY KEY,
                    source_signature TEXT NOT NULL,
                    model_kind TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    metrics_json TEXT,
                    created_at REAL NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_search_events (
                    id TEXT PRIMARY KEY,
                    user_scope_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    result_count INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    metadata_json TEXT,
                    occurred_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_recommendation_search_events_user_time "
                "ON recommendation_search_events(user_scope_id, occurred_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_recommendation_search_events_query_time "
                "ON recommendation_search_events(query, occurred_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recognition_match_events (
                    id TEXT PRIMARY KEY,
                    user_scope_id TEXT NOT NULL,
                    session_id TEXT,
                    source_type TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    recognition_status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    recognized_title TEXT,
                    recognized_artist TEXT,
                    resolved_track_id TEXT,
                    payload_json TEXT,
                    occurred_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_recognition_match_events_user_time "
                "ON recognition_match_events(user_scope_id, occurred_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_feature_store (
                    namespace TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    model_id TEXT,
                    payload_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(namespace, entity_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_experiment_assignments (
                    user_scope_id TEXT NOT NULL,
                    experiment_key TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    assigned_at REAL NOT NULL,
                    PRIMARY KEY(user_scope_id, experiment_key)
                )
                """
            )
            existing_assignment_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(recommendation_experiment_assignments)"
                ).fetchall()
            }
            if "assignment_source" not in existing_assignment_columns:
                connection.execute(
                    "ALTER TABLE recommendation_experiment_assignments "
                    "ADD COLUMN assignment_source TEXT NOT NULL DEFAULT 'hash_bucket'"
                )
            if "updated_at" not in existing_assignment_columns:
                connection.execute(
                    "ALTER TABLE recommendation_experiment_assignments "
                    "ADD COLUMN updated_at REAL"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_impressions (
                    id TEXT PRIMARY KEY,
                    user_scope_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    model_id TEXT,
                    experiment_key TEXT,
                    experiment_variant TEXT,
                    row_id TEXT,
                    track_id TEXT NOT NULL,
                    rank_index INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    payload_json TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_recommendation_impressions_user_time "
                "ON recommendation_impressions(user_scope_id, created_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_attributed_interactions (
                    interaction_id TEXT PRIMARY KEY,
                    impression_id TEXT,
                    user_scope_id TEXT NOT NULL,
                    session_id TEXT,
                    model_id TEXT,
                    experiment_key TEXT,
                    experiment_variant TEXT,
                    row_id TEXT,
                    track_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    weight REAL NOT NULL,
                    rank_index INTEGER,
                    occurred_at REAL NOT NULL,
                    payload_json TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_reco_attr_interactions_experiment_time "
                "ON recommendation_attributed_interactions(experiment_key, experiment_variant, occurred_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_reco_attr_interactions_user_time "
                "ON recommendation_attributed_interactions(user_scope_id, occurred_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_experiment_promotions (
                    id TEXT PRIMARY KEY,
                    experiment_key TEXT NOT NULL,
                    promoted_variant TEXT NOT NULL,
                    baseline_variant TEXT,
                    score REAL NOT NULL,
                    score_margin REAL NOT NULL,
                    impression_count INTEGER NOT NULL,
                    evaluation_window_hours INTEGER NOT NULL,
                    payload_json TEXT,
                    created_at REAL NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_reco_experiment_promotions_active "
                "ON recommendation_experiment_promotions(experiment_key, is_active, created_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_sync_state (
                    name TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()
    return resolved


def open_recommendation_store_connection_raw(server: Any):
    connection = sqlite3.connect(server.RECOMMENDATION_STORE_DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def open_recommendation_store_connection(server: Any | None = None):
    resolved = init_recommendation_store(server)
    return open_recommendation_store_connection_raw(resolved)


def open_recommendation_store_connection_without_init(server: Any | None = None):
    resolved = resolve_server(server)
    return open_recommendation_store_connection_raw(resolved)


def ensure_recommendation_store_initialized(server: Any | None = None) -> Any:
    return init_recommendation_store(server)


def open_recommendation_store_connection_initialized(server: Any | None = None):
    resolved = ensure_recommendation_store_initialized(server)
    return open_recommendation_store_connection_raw(resolved)
