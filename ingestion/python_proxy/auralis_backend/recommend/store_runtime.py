from __future__ import annotations

from typing import Any
import os
import sqlite3

from ..runtime_context import resolve_server as _resolve_runtime_server


def resolve_server(server: Any | None = None) -> Any:
    return _resolve_runtime_server(server)


def _store_path(server: Any) -> str:
    raw = str(server.RECOMMENDATION_STORE_DB_PATH or "").strip()
    return raw if raw == ":memory:" else os.path.abspath(raw)


def _store_is_initialized(server: Any) -> bool:
    path = _store_path(server)
    if str(getattr(server, "_recommendation_store_initialized_path", "")) != path:
        return False
    return path == ":memory:" or os.path.exists(path)


def init_recommendation_store(server: Any | None = None) -> Any:
    resolved = resolve_server(server)
    if _store_is_initialized(resolved):
        return resolved
    with resolved.recommendation_store_lock:
        if _store_is_initialized(resolved):
            return resolved
        connection = open_recommendation_store_connection_raw(resolved)
        try:
            if _store_path(resolved) != ":memory:":
                connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=15000")
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
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_canonical_entities (
                        entity_type TEXT NOT NULL,
                        entity_key TEXT NOT NULL,
                        title_key TEXT NOT NULL DEFAULT '',
                        artist_key TEXT NOT NULL DEFAULT '',
                        album_key TEXT NOT NULL DEFAULT '',
                        source_authority TEXT NOT NULL DEFAULT '',
                        source_quality REAL NOT NULL DEFAULT 0,
                        popularity REAL NOT NULL DEFAULT 0,
                        click_count INTEGER NOT NULL DEFAULT 0,
                        play_count INTEGER NOT NULL DEFAULT 0,
                        skip_count INTEGER NOT NULL DEFAULT 0,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(entity_type, entity_key)
                    )
                    """
                )
                existing_search_entity_columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(search_canonical_entities)"
                    ).fetchall()
                }
                if "official_source_provider" not in existing_search_entity_columns:
                    connection.execute(
                        "ALTER TABLE search_canonical_entities ADD COLUMN official_source_provider TEXT NOT NULL DEFAULT ''"
                    )
                if "official_source_key" not in existing_search_entity_columns:
                    connection.execute(
                        "ALTER TABLE search_canonical_entities ADD COLUMN official_source_key TEXT NOT NULL DEFAULT ''"
                    )
                if "official_source_authority" not in existing_search_entity_columns:
                    connection.execute(
                        "ALTER TABLE search_canonical_entities ADD COLUMN official_source_authority TEXT NOT NULL DEFAULT ''"
                    )
                if "official_confidence" not in existing_search_entity_columns:
                    connection.execute(
                        "ALTER TABLE search_canonical_entities ADD COLUMN official_confidence REAL NOT NULL DEFAULT 0"
                    )
                if "learned_popularity" not in existing_search_entity_columns:
                    connection.execute(
                        "ALTER TABLE search_canonical_entities ADD COLUMN learned_popularity REAL NOT NULL DEFAULT 0"
                    )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_search_canonical_entities_artist "
                    "ON search_canonical_entities(artist_key, updated_at DESC)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_search_canonical_entities_source "
                    "ON search_canonical_entities(official_source_provider, official_source_key, official_confidence DESC)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_query_memory (
                        user_scope_id TEXT NOT NULL,
                        query_key TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        entity_key TEXT NOT NULL,
                        title_key TEXT NOT NULL DEFAULT '',
                        artist_key TEXT NOT NULL DEFAULT '',
                        score REAL NOT NULL DEFAULT 0,
                        confidence REAL NOT NULL DEFAULT 0,
                        event_count INTEGER NOT NULL DEFAULT 0,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(user_scope_id, query_key, entity_type, entity_key)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_search_query_memory_lookup "
                    "ON search_query_memory(user_scope_id, query_key, score DESC, updated_at DESC)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_query_aliases (
                        alias_key TEXT NOT NULL,
                        canonical_query_key TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        entity_key TEXT NOT NULL,
                        title_key TEXT NOT NULL DEFAULT '',
                        artist_key TEXT NOT NULL DEFAULT '',
                        score REAL NOT NULL DEFAULT 0,
                        confidence REAL NOT NULL DEFAULT 0,
                        event_count INTEGER NOT NULL DEFAULT 0,
                        source TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(alias_key, entity_type, entity_key)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_search_query_aliases_lookup "
                    "ON search_query_aliases(alias_key, score DESC, confidence DESC, updated_at DESC)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_suggestion_cache (
                        cache_key TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_search_suggestion_cache_lru "
                    "ON search_suggestion_cache(updated_at ASC)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_snapshots (
                        snapshot_key TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        revision INTEGER NOT NULL DEFAULT 1,
                        last_accessed REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_search_snapshots_lru "
                    "ON search_snapshots(last_accessed ASC, updated_at ASC)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_snapshot_aliases (
                        alias_key TEXT NOT NULL,
                        search_mode TEXT NOT NULL,
                        snapshot_key TEXT NOT NULL,
                        confidence REAL NOT NULL DEFAULT 0,
                        source TEXT NOT NULL DEFAULT '',
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(alias_key, search_mode)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_search_snapshot_alias_target "
                    "ON search_snapshot_aliases(snapshot_key)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_source_identities (
                        source_provider TEXT NOT NULL,
                        source_key TEXT NOT NULL,
                        source_name TEXT NOT NULL DEFAULT '',
                        authority TEXT NOT NULL DEFAULT '',
                        confidence REAL NOT NULL DEFAULT 0,
                        evidence_json TEXT NOT NULL DEFAULT '{}',
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(source_provider, source_key)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_search_source_identities_authority "
                    "ON search_source_identities(authority, confidence DESC, updated_at DESC)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_entity_events (
                        entity_type TEXT NOT NULL,
                        entity_key TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        event_count INTEGER NOT NULL DEFAULT 0,
                        score REAL NOT NULL DEFAULT 0,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(entity_type, entity_key, event_type)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_search_entity_events_score "
                    "ON search_entity_events(entity_type, score DESC, updated_at DESC)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS catalog_entities (
                        entity_type TEXT NOT NULL,
                        entity_key TEXT NOT NULL,
                        display_title TEXT NOT NULL DEFAULT '',
                        display_artist TEXT NOT NULL DEFAULT '',
                        display_album TEXT NOT NULL DEFAULT '',
                        confidence REAL NOT NULL DEFAULT 0,
                        popularity REAL NOT NULL DEFAULT 0,
                        learned_popularity REAL NOT NULL DEFAULT 0,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(entity_type, entity_key)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_catalog_entities_type_popularity "
                    "ON catalog_entities(entity_type, learned_popularity DESC, popularity DESC, updated_at DESC)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS catalog_entity_aliases (
                        alias_key TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        entity_key TEXT NOT NULL,
                        score REAL NOT NULL DEFAULT 0,
                        confidence REAL NOT NULL DEFAULT 0,
                        source TEXT NOT NULL DEFAULT '',
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(alias_key, entity_type, entity_key)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_catalog_entity_aliases_lookup "
                    "ON catalog_entity_aliases(alias_key, score DESC, confidence DESC, updated_at DESC)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS catalog_entity_sources (
                        entity_type TEXT NOT NULL,
                        entity_key TEXT NOT NULL,
                        source_provider TEXT NOT NULL,
                        source_key TEXT NOT NULL,
                        source_authority TEXT NOT NULL DEFAULT '',
                        confidence REAL NOT NULL DEFAULT 0,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(entity_type, entity_key, source_provider, source_key)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_catalog_entity_sources_source "
                    "ON catalog_entity_sources(source_provider, source_key, confidence DESC)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS catalog_entity_metrics (
                        entity_type TEXT NOT NULL,
                        entity_key TEXT NOT NULL,
                        metric_name TEXT NOT NULL,
                        score REAL NOT NULL DEFAULT 0,
                        event_count INTEGER NOT NULL DEFAULT 0,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(entity_type, entity_key, metric_name)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_catalog_entity_metrics_score "
                    "ON catalog_entity_metrics(entity_type, metric_name, score DESC, updated_at DESC)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_catalog_backfill_events (
                        event_key TEXT PRIMARY KEY,
                        source TEXT NOT NULL DEFAULT '',
                        processed_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_search_catalog_backfill_events_source "
                    "ON search_catalog_backfill_events(source, processed_at DESC)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS external_catalog_import_queue (
                        seed_key TEXT PRIMARY KEY,
                        provider TEXT NOT NULL DEFAULT 'musicbrainz',
                        query TEXT NOT NULL,
                        seed_type TEXT NOT NULL DEFAULT 'query',
                        user_scope_id TEXT NOT NULL DEFAULT 'guest',
                        priority REAL NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        imported_count INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        processed_at REAL NOT NULL DEFAULT 0
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_external_catalog_import_queue_work "
                    "ON external_catalog_import_queue(provider, status, priority DESC, updated_at ASC)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_external_catalog_import_queue_scope "
                    "ON external_catalog_import_queue(user_scope_id, updated_at DESC)"
                )
            except sqlite3.OperationalError as exc:
                if "readonly" not in str(exc).lower():
                    raise
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
            resolved._recommendation_store_initialized_path = _store_path(resolved)
        finally:
            connection.close()
    return resolved


def open_recommendation_store_connection_raw(server: Any):
    connection = sqlite3.connect(
        server.RECOMMENDATION_STORE_DB_PATH,
        timeout=15.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=15000")
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
