from __future__ import annotations

from functools import wraps
from auralis_backend.runtime_context import resolve_server


def _with_server_globals(fn):
    @wraps(fn)
    def _wrapped(*args, **kwargs):
        server = resolve_server()
        for key, value in vars(server).items():
            if key.startswith('__'):
                continue
            globals()[key] = value
        return fn(*args, **kwargs)

    return _wrapped
@_with_server_globals
def _recommendation_external_pg_connection():
    if psycopg is None or not RECOMMENDATION_SYNC_DATABASE_DSN:
        return None
    connection = psycopg.connect(
        RECOMMENDATION_SYNC_DATABASE_DSN,
        connect_timeout=5,
    )
    connection.autocommit = True
    return connection


@_with_server_globals
def _recommendation_event_weight(event_type: Optional[str]) -> float:
    normalized = (event_type or "").strip().lower()
    if normalized == "complete":
        return 3.4
    if normalized == "download":
        return 3.0
    if normalized == "library":
        return 3.2
    if normalized == "playlist_add":
        return 3.3
    if normalized == "repeat":
        return 2.4
    if normalized == "impression":
        return 0.0
    if normalized == "tab_tap":
        return 0.0
    if normalized == "skip":
        return -2.0
    return 1.0


@_with_server_globals
def _recommendation_sync_state_get(name: str, default: str = "0") -> str:
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        row = connection.execute(
            "SELECT value FROM recommendation_sync_state WHERE name = ?",
            [name],
        ).fetchone()
        if row is None:
            return default
        value = (row["value"] or "").strip()
        return value or default
    finally:
        connection.close()


@_with_server_globals
def _recommendation_sync_state_set(name: str, value: str):
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        connection.execute(
            """
            INSERT INTO recommendation_sync_state(name, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            [name, value, time.time()],
        )
        connection.commit()
    finally:
        connection.close()


@_with_server_globals
def _recommendation_sync_state_float(name: str, default: float = 0.0) -> float:
    try:
        return float(_recommendation_sync_state_get(name, str(default)) or default)
    except Exception:
        return default


@_with_server_globals
def _recommendation_mark_external_sync_failure(message: str):
    status, reachable = _recommendation_classify_external_sync_error(message)
    _recommendation_sync_state_set("external_sync_status", status)
    _recommendation_sync_state_set("external_sync_reachable", "1" if reachable else "0")
    _recommendation_sync_state_set(
        "external_last_sync_error",
        (message or "unknown external sync error")[:1000],
    )
    _recommendation_sync_state_set(
        "external_last_sync_failure_at",
        str(time.time()),
    )
    _recommendation_sync_state_set(
        "external_last_sync_attempt_at",
        str(time.time()),
    )


@_with_server_globals
def _recommendation_clear_external_sync_failure():
    _recommendation_sync_state_set("external_sync_status", "reachable")
    _recommendation_sync_state_set("external_sync_reachable", "1")
    _recommendation_sync_state_set("external_last_sync_error", "")
    _recommendation_sync_state_set("external_last_sync_failure_at", "0")


@_with_server_globals
def _recommendation_classify_external_sync_error(message: str) -> tuple[str, bool]:
    normalized = _recommendation_trim_text(message).lower()
    if not normalized:
        return "connection_error", False
    if any(
        token in normalized
        for token in [
            "authentication failed",
            "password authentication failed",
            "auth failed",
            "too many authentication errors",
            "circuit breaker open",
        ]
    ):
        return "auth_failed", True
    if any(
        token in normalized
        for token in [
            "connection timeout",
            "timeout expired",
            "timed out",
            "getaddrinfo failed",
            "name or service not known",
            "temporary failure in name resolution",
        ]
    ):
        return "connect_timeout", False
    return "connection_error", False


@_with_server_globals
def _recommendation_mark_external_sync_success():
    now = str(time.time())
    _recommendation_sync_state_set("external_sync_status", "reachable")
    _recommendation_sync_state_set("external_sync_reachable", "1")
    _recommendation_sync_state_set("external_last_sync_attempt_at", now)
    _recommendation_sync_state_set("external_last_sync_success_at", now)
    _recommendation_clear_external_sync_failure()


@_with_server_globals
def _recommendation_external_sync_health_snapshot():
    status = _recommendation_trim_text(
        _recommendation_sync_state_get(
            "external_sync_status",
            "disabled" if not RECOMMENDATION_SYNC_DATABASE_DSN else "unknown",
        )
    ) or ("disabled" if not RECOMMENDATION_SYNC_DATABASE_DSN else "unknown")
    last_error = _recommendation_trim_text(
        _recommendation_sync_state_get("external_last_sync_error", "")
    )
    last_error_at = _recommendation_sync_state_float("external_last_sync_failure_at", 0.0)
    last_success_at = _recommendation_sync_state_float("external_last_sync_success_at", 0.0)
    last_attempt_at = _recommendation_sync_state_float("external_last_sync_attempt_at", 0.0)
    reachable = status in {"reachable", "auth_failed"}
    return {
        "status": status,
        "reachable": reachable,
        "auth_failed": status == "auth_failed",
        "connect_timeout": status == "connect_timeout",
        "dsn_configured": bool(RECOMMENDATION_SYNC_DATABASE_DSN),
        "last_success_at": last_success_at,
        "last_error_at": last_error_at,
        "last_attempt_at": last_attempt_at,
        "last_error": last_error,
    }


@_with_server_globals
def _recommendation_should_retry_external_sync(force: bool = False) -> bool:
    if force or RECOMMENDATION_SYNC_FAILURE_RETRY_SECONDS <= 0:
        return True
    last_error = _recommendation_trim_text(
        _recommendation_sync_state_get("external_last_sync_error", "")
    )
    if not last_error:
        return True
    last_failure_at = _recommendation_sync_state_float(
        "external_last_sync_failure_at",
        0.0,
    )
    if last_failure_at <= 0:
        return True
    return (time.time() - last_failure_at) >= RECOMMENDATION_SYNC_FAILURE_RETRY_SECONDS


@_with_server_globals
def _recommendation_active_promotion():
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        row = connection.execute(
            """
            SELECT id, promoted_variant, baseline_variant, score, score_margin,
                   impression_count, evaluation_window_hours, payload_json, created_at
            FROM recommendation_experiment_promotions
            WHERE experiment_key = ? AND is_active = 1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [RECOMMENDATION_EXPERIMENT_KEY],
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        promoted_variant = _recommendation_trim_text(
            _recommendation_sync_state_get("experiment_promoted_variant", "")
        )
        if not promoted_variant:
            return None
        return {
            "promotion_id": "",
            "promoted_variant": promoted_variant,
            "baseline_variant": _recommendation_sync_state_get(
                "experiment_promotion_baseline",
                "",
            ),
            "score": _recommendation_sync_state_float("experiment_promotion_score", 0.0),
            "score_margin": _recommendation_sync_state_float(
                "experiment_promotion_score_margin",
                0.0,
            ),
            "impression_count": int(
                _recommendation_sync_state_float(
                    "experiment_promotion_impression_count",
                    0.0,
                )
            ),
            "evaluation_window_hours": int(
                _recommendation_sync_state_float(
                    "experiment_promotion_window_hours",
                    float(RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS),
                )
            ),
            "payload": {},
            "created_at": _recommendation_sync_state_float(
                "experiment_promoted_at",
                0.0,
            ),
        }
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        payload = {}
    return {
        "promotion_id": row["id"] or "",
        "promoted_variant": row["promoted_variant"] or "",
        "baseline_variant": row["baseline_variant"] or "",
        "score": float(row["score"] or 0.0),
        "score_margin": float(row["score_margin"] or 0.0),
        "impression_count": int(row["impression_count"] or 0),
        "evaluation_window_hours": int(row["evaluation_window_hours"] or 0),
        "payload": payload,
        "created_at": float(row["created_at"] or 0.0),
    }


@_with_server_globals
def _recommendation_find_recent_impression(
    user_scope_id: str,
    track_id: str,
    occurred_at: float,
):
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        return connection.execute(
            """
            SELECT id, session_id, model_id, experiment_key, experiment_variant,
                   row_id, rank_index, created_at
            FROM recommendation_impressions
            WHERE user_scope_id = ? AND track_id = ?
              AND created_at <= ? AND created_at >= ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [
                user_scope_id,
                track_id,
                occurred_at,
                occurred_at - RECOMMENDATION_ATTRIBUTION_WINDOW_SECONDS,
            ],
        ).fetchone()
    finally:
        connection.close()


@_with_server_globals
def _recommendation_attribute_interaction_event(
    interaction_id: str,
    *,
    user_scope_id: str,
    track_id: str,
    event_type: str,
    weight: float,
    occurred_at: float,
    payload: Optional[Dict[str, Any]] = None,
):
    impression = _recommendation_find_recent_impression(
        user_scope_id,
        track_id,
        occurred_at,
    )
    if impression is None:
        return False
    payload_json = json.dumps(
        {
            **(payload or {}),
            "impression_created_at": float(impression["created_at"] or 0.0),
            "attribution_latency_seconds": max(
                0.0,
                float(occurred_at) - float(impression["created_at"] or 0.0),
            ),
        },
        ensure_ascii=False,
    )
    connection = _recommendation_store_connection()
    try:
        connection.execute(
            """
            INSERT OR IGNORE INTO recommendation_attributed_interactions(
                interaction_id, impression_id, user_scope_id, session_id, model_id,
                experiment_key, experiment_variant, row_id, track_id, event_type,
                weight, rank_index, occurred_at, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                interaction_id,
                impression["id"] or None,
                user_scope_id,
                impression["session_id"] or None,
                impression["model_id"] or None,
                impression["experiment_key"] or None,
                impression["experiment_variant"] or None,
                impression["row_id"] or None,
                track_id,
                event_type,
                float(weight),
                int(impression["rank_index"] or 0),
                float(occurred_at),
                payload_json,
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return True


@_with_server_globals
def _recommendation_experiment_dashboard(window_hours: int = None):
    evaluation_window_hours = max(
        1,
        int(window_hours or RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS),
    )
    cutoff = time.time() - (evaluation_window_hours * 3600)
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    variants = {}
    model_breakdown = []
    try:
        impression_rows = connection.execute(
            """
            SELECT experiment_variant, COUNT(*) AS impression_count,
                   COUNT(DISTINCT user_scope_id) AS user_count,
                   COUNT(DISTINCT session_id) AS session_count
            FROM recommendation_impressions
            WHERE experiment_key = ? AND created_at >= ?
            GROUP BY experiment_variant
            ORDER BY impression_count DESC
            """,
            [RECOMMENDATION_EXPERIMENT_KEY, cutoff],
        ).fetchall()
        for row in impression_rows:
            variant = _recommendation_trim_text(row["experiment_variant"] or "unknown")
            variants[variant] = {
                "variant": variant,
                "impression_count": int(row["impression_count"] or 0),
                "user_count": int(row["user_count"] or 0),
                "session_count": int(row["session_count"] or 0),
                "interaction_counts": {},
                "engaged_impression_count": 0,
                "weighted_outcome": 0.0,
            }

        interaction_rows = connection.execute(
            """
            SELECT experiment_variant, event_type,
                   COUNT(*) AS event_count,
                   COUNT(DISTINCT impression_id) AS engaged_impression_count,
                   COALESCE(SUM(weight), 0) AS weighted_outcome
            FROM recommendation_attributed_interactions
            WHERE experiment_key = ? AND occurred_at >= ?
            GROUP BY experiment_variant, event_type
            ORDER BY experiment_variant ASC, event_type ASC
            """,
            [RECOMMENDATION_EXPERIMENT_KEY, cutoff],
        ).fetchall()
        for row in interaction_rows:
            variant = _recommendation_trim_text(row["experiment_variant"] or "unknown")
            variant_entry = variants.setdefault(
                variant,
                {
                    "variant": variant,
                    "impression_count": 0,
                    "user_count": 0,
                    "session_count": 0,
                    "interaction_counts": {},
                    "engaged_impression_count": 0,
                    "weighted_outcome": 0.0,
                },
            )
            event_type = _recommendation_trim_text(row["event_type"] or "unknown")
            variant_entry["interaction_counts"][event_type] = int(row["event_count"] or 0)
            if event_type in {"play", "complete", "library", "download"}:
                variant_entry["engaged_impression_count"] += int(
                    row["engaged_impression_count"] or 0
                )
            variant_entry["weighted_outcome"] += float(row["weighted_outcome"] or 0.0)

        model_rows = connection.execute(
            """
            SELECT experiment_variant, COALESCE(model_id, '') AS model_id,
                   COUNT(*) AS impression_count
            FROM recommendation_impressions
            WHERE experiment_key = ? AND created_at >= ?
            GROUP BY experiment_variant, COALESCE(model_id, '')
            ORDER BY impression_count DESC
            LIMIT 16
            """,
            [RECOMMENDATION_EXPERIMENT_KEY, cutoff],
        ).fetchall()
        for row in model_rows:
            model_breakdown.append(
                {
                    "variant": _recommendation_trim_text(
                        row["experiment_variant"] or "unknown"
                    ),
                    "model_id": _recommendation_trim_text(row["model_id"]),
                    "impression_count": int(row["impression_count"] or 0),
                }
            )
    finally:
        connection.close()

    ranked_variants = []
    for variant in variants.values():
        impressions = max(int(variant.get("impression_count") or 0), 0)
        interactions = variant.get("interaction_counts") or {}
        engaged_impressions = min(
            impressions,
            max(int(variant.get("engaged_impression_count") or 0), 0),
        )
        weighted_outcome = float(variant.get("weighted_outcome") or 0.0)
        play_count = int(interactions.get("play") or 0)
        complete_count = int(interactions.get("complete") or 0)
        library_count = int(interactions.get("library") or 0)
        download_count = int(interactions.get("download") or 0)
        skip_count = int(interactions.get("skip") or 0)
        variant["engagement_rate"] = round(
            (engaged_impressions / impressions) if impressions else 0.0,
            4,
        )
        variant["play_rate"] = round(
            (play_count / impressions) if impressions else 0.0,
            4,
        )
        variant["completion_rate"] = round(
            (complete_count / impressions) if impressions else 0.0,
            4,
        )
        variant["save_rate"] = round(
            ((library_count + download_count) / impressions) if impressions else 0.0,
            4,
        )
        variant["skip_rate"] = round(
            (skip_count / impressions) if impressions else 0.0,
            4,
        )
        variant["score_per_impression"] = round(
            (weighted_outcome / impressions) if impressions else 0.0,
            4,
        )
        variant["weighted_outcome"] = round(weighted_outcome, 4)
        ranked_variants.append(variant)

    ranked_variants.sort(
        key=lambda item: (
            float(item.get("score_per_impression") or 0.0),
            float(item.get("engagement_rate") or 0.0),
            int(item.get("impression_count") or 0),
        ),
        reverse=True,
    )
    active_promotion = _recommendation_active_promotion()
    return {
        "experiment_key": RECOMMENDATION_EXPERIMENT_KEY,
        "evaluation_window_hours": evaluation_window_hours,
        "promotion_enabled": RECOMMENDATION_PROMOTE_WINNER,
        "minimum_impressions": RECOMMENDATION_EXPERIMENT_MIN_IMPRESSIONS,
        "minimum_margin": RECOMMENDATION_EXPERIMENT_MIN_SCORE_MARGIN,
        "active_promotion": active_promotion,
        "variants": ranked_variants,
        "model_breakdown": model_breakdown,
    }


@_with_server_globals
def _recommendation_evaluate_experiment(*, force_promote: bool = False, window_hours: int = None):
    dashboard = _recommendation_experiment_dashboard(window_hours=window_hours)
    variants = list(dashboard.get("variants") or [])
    eligible_variants = [
        variant
        for variant in variants
        if int(variant.get("impression_count") or 0) >= RECOMMENDATION_EXPERIMENT_MIN_IMPRESSIONS
    ]
    if len(eligible_variants) < 2:
        return {
            "evaluated": False,
            "promoted": False,
            "reason": "insufficient_impressions",
            "dashboard": dashboard,
        }

    winner = eligible_variants[0]
    runner_up = eligible_variants[1]
    score_margin = round(
        float(winner.get("score_per_impression") or 0.0)
        - float(runner_up.get("score_per_impression") or 0.0),
        4,
    )
    if not force_promote and score_margin < RECOMMENDATION_EXPERIMENT_MIN_SCORE_MARGIN:
        return {
            "evaluated": True,
            "promoted": False,
            "reason": "margin_too_small",
            "score_margin": score_margin,
            "winner": winner,
            "runner_up": runner_up,
            "dashboard": dashboard,
        }

    active_promotion = dashboard.get("active_promotion") or {}
    promoted_variant = _recommendation_trim_text(winner.get("variant"))
    if (
        promoted_variant
        and promoted_variant == _recommendation_trim_text(active_promotion.get("promoted_variant"))
    ):
        return {
            "evaluated": True,
            "promoted": False,
            "reason": "winner_already_active",
            "winner": winner,
            "runner_up": runner_up,
            "score_margin": score_margin,
            "dashboard": dashboard,
        }

    promotion_id = str(uuid.uuid4())
    created_at = time.time()
    payload = {
        "winner": winner,
        "runner_up": runner_up,
        "dashboard": dashboard,
    }
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        connection.execute(
            """
            UPDATE recommendation_experiment_promotions
            SET is_active = 0
            WHERE experiment_key = ?
            """,
            [RECOMMENDATION_EXPERIMENT_KEY],
        )
        connection.execute(
            """
            INSERT INTO recommendation_experiment_promotions(
                id, experiment_key, promoted_variant, baseline_variant, score,
                score_margin, impression_count, evaluation_window_hours,
                payload_json, created_at, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            [
                promotion_id,
                RECOMMENDATION_EXPERIMENT_KEY,
                promoted_variant,
                _recommendation_trim_text(runner_up.get("variant")) or None,
                float(winner.get("score_per_impression") or 0.0),
                score_margin,
                int(winner.get("impression_count") or 0),
                int(dashboard.get("evaluation_window_hours") or RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS),
                json.dumps(payload, ensure_ascii=False),
                created_at,
            ],
        )
        connection.commit()
    finally:
        connection.close()

    _recommendation_sync_state_set("experiment_promoted_variant", promoted_variant)
    _recommendation_sync_state_set(
        "experiment_promotion_baseline",
        _recommendation_trim_text(runner_up.get("variant")),
    )
    _recommendation_sync_state_set(
        "experiment_promotion_score",
        str(float(winner.get("score_per_impression") or 0.0)),
    )
    _recommendation_sync_state_set(
        "experiment_promotion_score_margin",
        str(score_margin),
    )
    _recommendation_sync_state_set(
        "experiment_promotion_impression_count",
        str(int(winner.get("impression_count") or 0)),
    )
    _recommendation_sync_state_set(
        "experiment_promotion_window_hours",
        str(int(dashboard.get("evaluation_window_hours") or RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS)),
    )
    _recommendation_sync_state_set("experiment_promoted_at", str(created_at))
    return {
        "evaluated": True,
        "promoted": True,
        "winner": winner,
        "runner_up": runner_up,
        "score_margin": score_margin,
        "promotion_id": promotion_id,
        "dashboard": _recommendation_experiment_dashboard(window_hours=window_hours),
    }


@_with_server_globals
def _recommendation_runtime_snapshot(version_limit: int = 5):
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    feature_store_counts = {}
    recent_versions = []
    impressions_by_variant = {}
    attributed_interactions_by_variant = {}
    model_version_count = 0
    try:
        feature_rows = connection.execute(
            """
            SELECT namespace, COUNT(*) AS row_count
            FROM recommendation_feature_store
            GROUP BY namespace
            ORDER BY namespace ASC
            """
        ).fetchall()
        for row in feature_rows:
            namespace = _recommendation_trim_text(row["namespace"])
            if not namespace:
                continue
            feature_store_counts[namespace] = int(row["row_count"] or 0)

        version_count_row = connection.execute(
            "SELECT COUNT(*) AS version_count FROM recommendation_model_versions"
        ).fetchone()
        model_version_count = int((version_count_row["version_count"] or 0) if version_count_row else 0)
        version_rows = connection.execute(
            """
            SELECT id, model_kind, created_at, is_active, metrics_json
            FROM recommendation_model_versions
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [max(1, min(int(version_limit or 5), 24))],
        ).fetchall()
        for row in version_rows:
            try:
                metrics = json.loads(row["metrics_json"] or "{}")
            except Exception:
                metrics = {}
            recent_versions.append(
                {
                    "model_id": row["id"] or "",
                    "model_kind": row["model_kind"] or "",
                    "created_at": float(row["created_at"] or 0),
                    "is_active": bool(row["is_active"]),
                    "metrics": metrics,
                }
            )

        variant_rows = connection.execute(
            """
            SELECT experiment_variant, COUNT(*) AS impression_count
            FROM recommendation_impressions
            GROUP BY experiment_variant
            ORDER BY impression_count DESC
            """
        ).fetchall()
        for row in variant_rows:
            variant = _recommendation_trim_text(row["experiment_variant"] or "unknown")
            impressions_by_variant[variant or "unknown"] = int(
                row["impression_count"] or 0
            )

        attributed_rows = connection.execute(
            """
            SELECT experiment_variant, COUNT(*) AS interaction_count
            FROM recommendation_attributed_interactions
            GROUP BY experiment_variant
            ORDER BY interaction_count DESC
            """
        ).fetchall()
        for row in attributed_rows:
            variant = _recommendation_trim_text(row["experiment_variant"] or "unknown")
            attributed_interactions_by_variant[variant or "unknown"] = int(
                row["interaction_count"] or 0
            )
    finally:
        connection.close()

    active_promotion = _recommendation_active_promotion()

    return {
        "feature_store_counts": feature_store_counts,
        "model_version_count": model_version_count,
        "recent_versions": recent_versions,
        "impressions_by_variant": impressions_by_variant,
        "attributed_interactions_by_variant": attributed_interactions_by_variant,
        "external_worker_expected": RECOMMENDATION_EXTERNAL_WORKER,
        "last_external_sync_at": _recommendation_sync_state_float("external_last_sync_at", 0.0),
        "last_external_synced_count": int(
            _recommendation_sync_state_float("external_last_synced_count", 0.0)
        ),
        "last_external_sync_error": _recommendation_sync_state_get(
            "external_last_sync_error",
            "",
        ),
        "last_external_sync_failure_at": _recommendation_sync_state_float(
            "external_last_sync_failure_at",
            0.0,
        ),
        "external_sync_failure_retry_seconds": RECOMMENDATION_SYNC_FAILURE_RETRY_SECONDS,
        "last_scheduler_sync_at": _recommendation_sync_state_float(
            "scheduler_last_sync_at",
            0.0,
        ),
        "last_scheduler_train_at": _recommendation_sync_state_float(
            "scheduler_last_train_at",
            0.0,
        ),
        "last_scheduler_error": _recommendation_sync_state_get(
            "scheduler_last_error",
            "",
        ),
        "worker_mode": _recommendation_sync_state_get("worker_mode", ""),
        "worker_status": _recommendation_sync_state_get("worker_status", ""),
        "worker_process_id": _recommendation_sync_state_get("worker_process_id", ""),
        "worker_started_at": _recommendation_sync_state_float("worker_started_at", 0.0),
        "worker_last_heartbeat_at": _recommendation_sync_state_float(
            "worker_last_heartbeat_at",
            0.0,
        ),
        "last_trained_signature": _recommendation_sync_state_get(
            "scheduler_last_trained_signature",
            "",
        ),
        "active_promotion": active_promotion,
        "export_dir": RECOMMENDATION_MODEL_EXPORT_DIR,
    }


@_with_server_globals
def _recommendation_store_search_event(req: RecommendationSearchEventRequest):
    _recommendation_init_store_db()
    query = _recommendation_trim_text(req.query)
    if not query:
        return False
    user_scope_id = _assistant_safe_scope_id(req.user_scope_id or "guest")
    occurred_at = float(req.occurred_at or time.time())
    payload = dict(req.metadata or {})
    event_id = hashlib.sha1(
        json.dumps(
            {
                "user_scope_id": user_scope_id,
                "query": query,
                "occurred_at": round(occurred_at, 3),
                "source": req.source or "app",
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    connection = _recommendation_store_connection()
    try:
        connection.execute(
            """
            INSERT OR IGNORE INTO recommendation_search_events(
                id, user_scope_id, query, result_count, source, metadata_json, occurred_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event_id,
                user_scope_id,
                query,
                max(int(req.result_count or 0), 0),
                _recommendation_trim_text(req.source or "app") or "app",
                json.dumps(payload, ensure_ascii=False),
                occurred_at,
            ],
        )
        connection.commit()
    finally:
        connection.close()
    selected_item = {}
    selected_type = ""
    for key in (
        "selected_item",
        "clicked_item",
        "top_result_item",
        "track",
        "item",
        "entity",
    ):
        value = payload.get(key)
        if isinstance(value, dict):
            selected_item = dict(value)
            break
    selected_type = _recommendation_trim_text(
        payload.get("selected_entity_type")
        or payload.get("clicked_entity_type")
        or payload.get("entity_type")
        or ("track" if selected_item else "")
    )
    if selected_item:
        try:
            from ..search.intelligence import remember_search_resolution

            remember_search_resolution(
                resolve_server(),
                user_scope_id=user_scope_id,
                query=query,
                entity_type=selected_type or "track",
                item=selected_item,
                confidence=float(payload.get("confidence") or 0.8),
                event_weight=max(_recommendation_event_weight(payload.get("event_type") or req.source), 1.0),
                event_type=_recommendation_trim_text(payload.get("event_type") or req.source),
                source="search_interaction",
            )
        except Exception:
            pass
        try:
            from ..discovery.inventory import append_inventory_intent_delta

            intent_version = append_inventory_intent_delta(
                resolve_server(),
                user_scope_id=user_scope_id,
                item=selected_item,
                entity_type=selected_type or "track",
                query=query,
            )
            if intent_version > 0:
                payload["inventory_intent_version"] = intent_version
        except Exception:
            pass
    _recommendation_invalidate_collaborative_cache()
    try:
        from ..discovery.feed_state import load_feed_state, mark_feed_dirty

        feed_state = load_feed_state(resolve_server(), user_scope_id)
        if feed_state is not None:
            mark_feed_dirty(resolve_server(), feed_state, "search_interaction")
    except Exception:
        pass
    return True


@_with_server_globals
def _recommendation_feature_store_upsert_many(rows):
    if not rows:
        return
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        connection.executemany(
            """
            INSERT INTO recommendation_feature_store(namespace, entity_id, model_id, payload_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(namespace, entity_id) DO UPDATE SET
                model_id = excluded.model_id,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        connection.commit()
    finally:
        connection.close()


@_with_server_globals
def _recommendation_assignment_for_user(user_scope_id: str):
    _recommendation_init_store_db()
    normalized_user_scope_id = _assistant_safe_scope_id(user_scope_id or "guest")
    active_promotion = _recommendation_active_promotion() or {}
    promoted_variant = _recommendation_trim_text(active_promotion.get("promoted_variant"))
    connection = _recommendation_store_connection()
    try:
        row = connection.execute(
            """
            SELECT variant, assignment_source
            FROM recommendation_experiment_assignments
            WHERE user_scope_id = ? AND experiment_key = ?
            """,
            [normalized_user_scope_id, RECOMMENDATION_EXPERIMENT_KEY],
        ).fetchone()
        if promoted_variant:
            if (
                row is None
                or _recommendation_trim_text(row["variant"]) != promoted_variant
                or _recommendation_trim_text(row["assignment_source"]) != "promotion"
            ):
                connection.execute(
                    """
                    INSERT INTO recommendation_experiment_assignments(
                        user_scope_id, experiment_key, variant, assigned_at, assignment_source, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_scope_id, experiment_key) DO UPDATE SET
                        variant = excluded.variant,
                        assignment_source = excluded.assignment_source,
                        updated_at = excluded.updated_at
                    """,
                    [
                        normalized_user_scope_id,
                        RECOMMENDATION_EXPERIMENT_KEY,
                        promoted_variant,
                        time.time(),
                        "promotion",
                        time.time(),
                    ],
                )
                connection.commit()
            return promoted_variant

        if row is not None:
            existing_source = _recommendation_trim_text(row["assignment_source"])
            if existing_source != "promotion":
                return (row["variant"] or "control").strip() or "control"

        digest = hashlib.sha1(
            f"{RECOMMENDATION_EXPERIMENT_KEY}:{normalized_user_scope_id}".encode("utf-8")
        ).hexdigest()
        bucket = int(digest[:8], 16) % 100
        variant = "collab_heavy" if bucket >= 50 else "control"
        connection.execute(
            """
            INSERT INTO recommendation_experiment_assignments(
                user_scope_id, experiment_key, variant, assigned_at, assignment_source, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_scope_id, experiment_key) DO UPDATE SET
                variant = excluded.variant,
                assignment_source = excluded.assignment_source,
                updated_at = excluded.updated_at
            """,
            [
                normalized_user_scope_id,
                RECOMMENDATION_EXPERIMENT_KEY,
                variant,
                time.time(),
                "hash_bucket",
                time.time(),
            ],
        )
        connection.commit()
        return variant
    finally:
        connection.close()


@_with_server_globals
def _recommendation_record_impressions(session, rows):
    if not isinstance(session, dict):
        return
    if not rows:
        return
    _recommendation_init_store_db()
    session_id = _recommendation_trim_text(session.get("session_id"))
    user_scope_id = _assistant_safe_scope_id(session.get("user_scope_id") or "guest")
    diagnostics = session.get("diagnostics") or {}
    model_id = _recommendation_trim_text(diagnostics.get("collaborative_model_id"))
    variant = _recommendation_trim_text(diagnostics.get("experiment_variant") or "control")
    created_at = time.time()
    payload_rows = []
    for row in rows:
        row_id = _recommendation_trim_text(row.get("id"))
        for index, item in enumerate(row.get("items") or []):
            track_id = _recommendation_trim_text(item.get("id"))
            if not track_id:
                continue
            impression_id = hashlib.sha1(
                f"{session_id}:{row_id}:{track_id}:{index}".encode("utf-8")
            ).hexdigest()
            payload_rows.append(
                [
                    impression_id,
                    user_scope_id,
                    session_id,
                    model_id or None,
                    RECOMMENDATION_EXPERIMENT_KEY,
                    variant,
                    row_id or None,
                    track_id,
                    index,
                    created_at,
                    json.dumps(
                        {
                            "generator_score": item.get("generator_score"),
                            "ml_similarities": item.get("ml_similarities") or {},
                        },
                        ensure_ascii=False,
                    ),
                ]
            )
    if not payload_rows:
        return
    connection = _recommendation_store_connection()
    try:
        connection.executemany(
            """
            INSERT OR IGNORE INTO recommendation_impressions(
                id, user_scope_id, session_id, model_id, experiment_key,
                experiment_variant, row_id, track_id, rank_index, created_at, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload_rows,
        )
        connection.commit()
    finally:
        connection.close()


@_with_server_globals
def _recommendation_invalidate_collaborative_cache():
    from auralis_backend.recommend.model_runtime import invalidate_model_cache
    from auralis_backend.recommend.profile_runtime import invalidate_profile_cache
    from auralis_backend.recommend.session_runtime import clear_feed_sessions

    invalidate_model_cache()
    invalidate_profile_cache()
    clear_feed_sessions(sys.modules[__name__])
    _cache_clear_namespace(
        search_result_cache,
        search_result_cache_lock,
        "recommended_artists",
    )


@_with_server_globals
def _recommendation_store_interaction_event(req: RecommendationInteractionEventRequest):
    _recommendation_init_store_db()
    track_id = _recommendation_trim_text(req.track_id)
    if not track_id:
        return False
    user_scope_id = _assistant_safe_scope_id(req.user_scope_id or "guest")
    event_type = (req.event_type or "play").strip().lower() or "play"
    artist_name = _recommendation_trim_text(
        req.artist_name
        or (req.metadata or {}).get("channel")
        or (req.metadata or {}).get("artist")
        or (req.metadata or {}).get("author")
    )
    occurred_at = float(req.occurred_at or time.time())
    payload = dict(req.metadata or {})
    payload.setdefault("track_id", track_id)
    event_id = hashlib.sha1(
        json.dumps(
            {
                "user_scope_id": user_scope_id,
                "track_id": track_id,
                "event_type": event_type,
                "occurred_at": round(occurred_at, 3),
                "source": req.source or "app",
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    connection = _recommendation_store_connection()
    try:
        connection.execute(
            """
            INSERT OR IGNORE INTO recommendation_events(
                id, user_scope_id, track_id, artist_name, event_type, source, weight, metadata_json, occurred_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event_id,
                user_scope_id,
                track_id,
                artist_name or None,
                event_type,
                _recommendation_trim_text(req.source or "app") or "app",
                _recommendation_event_weight(event_type),
                json.dumps(payload, ensure_ascii=False),
                occurred_at,
            ],
        )
        connection.commit()
    finally:
        connection.close()
    try:
        from ..domain.user_state import schedule_history_seed_snapshot_refresh

        schedule_history_seed_snapshot_refresh(resolve_server(), user_scope_id)
    except Exception:
        pass
    _recommendation_attribute_interaction_event(
        event_id,
        user_scope_id=user_scope_id,
        track_id=track_id,
        event_type=event_type,
        weight=_recommendation_event_weight(event_type),
        occurred_at=occurred_at,
        payload=payload,
    )
    try:
        from auralis_backend.recommend.profile_runtime import invalidate_profile_cache
        from auralis_backend.recommend.taste_runtime import apply_interaction_feedback

        apply_interaction_feedback(sys.modules[__name__], req)
        invalidate_profile_cache()
    except Exception:
        pass
    _recommendation_invalidate_collaborative_cache()
    if event_type in {"play", "complete", "save", "playlist_add"} and (
        "search" in _recommendation_trim_text(req.source).lower()
        or "search" in _recommendation_trim_text(payload.get("recommendation_origin")).lower()
    ):
        try:
            from ..discovery.inventory import append_inventory_intent_delta

            append_inventory_intent_delta(
                resolve_server(),
                user_scope_id=user_scope_id,
                item={**payload, "id": track_id, "artist": artist_name},
                entity_type="track",
                query=_recommendation_trim_text(payload.get("search_query")),
            )
        except Exception:
            pass
    try:
        from ..discovery.feed_state import load_feed_state, mark_feed_dirty

        feed_state = load_feed_state(resolve_server(), user_scope_id)
        if feed_state is not None:
            mark_feed_dirty(resolve_server(), feed_state, f"interaction:{event_type}")
    except Exception:
        pass
    return True


@_with_server_globals
def _recommendation_sync_external_events(force: bool = False):
    _recommendation_init_store_db()
    if psycopg is None or not RECOMMENDATION_SYNC_DATABASE_DSN:
        _recommendation_sync_state_set("external_sync_status", "disabled")
        _recommendation_sync_state_set("external_sync_reachable", "0")
        return {"synced": 0, "enabled": False}

    _recommendation_sync_state_set("external_last_sync_attempt_at", str(time.time()))
    if not _recommendation_should_retry_external_sync(force=force):
        return {
            "synced": 0,
            "enabled": True,
            "skipped": True,
            "reason": "recent_external_sync_error",
            "error": _recommendation_sync_state_get("external_last_sync_error", ""),
            "sync_health": _recommendation_external_sync_health_snapshot(),
        }

    try:
        connection = _recommendation_external_pg_connection()
    except Exception as exc:
        _recommendation_mark_external_sync_failure(str(exc))
        return {
            "synced": 0,
            "enabled": True,
            "error": str(exc)[:1000],
            "sync_health": _recommendation_external_sync_health_snapshot(),
        }
    if connection is None:
        _recommendation_mark_external_sync_failure("psycopg connection unavailable")
        return {
            "synced": 0,
            "enabled": False,
            "sync_health": _recommendation_external_sync_health_snapshot(),
        }

    local_connection = _recommendation_store_connection()
    total_synced = 0
    last_play_ts = float(_recommendation_sync_state_get("external_play_ts", "0") or 0)
    last_library_ts = float(_recommendation_sync_state_get("external_library_ts", "0") or 0)
    last_search_ts = float(_recommendation_sync_state_get("external_search_ts", "0") or 0)
    sync_succeeded = False

    try:
        with connection.cursor() as cursor:
            while True:
                cursor.execute(
                    """
                    SELECT id::text, user_id::text, track_id, event_type,
                           COALESCE(metadata::text, '{}'),
                           EXTRACT(EPOCH FROM created_at)
                    FROM public.play_events
                    WHERE created_at > to_timestamp(%s)
                    ORDER BY created_at ASC
                    LIMIT %s
                    """,
                    [last_play_ts, RECOMMENDATION_SYNC_BATCH_SIZE],
                )
                rows = cursor.fetchall()
                if not rows:
                    break
                for row in rows:
                    event_id, user_id, track_id, event_type, metadata_json, created_at = row
                    normalized_track_id = _recommendation_trim_text(track_id)
                    if not normalized_track_id:
                        continue
                    local_connection.execute(
                        """
                        INSERT OR IGNORE INTO recommendation_events(
                            id, user_scope_id, track_id, artist_name, event_type, source, weight, metadata_json, occurred_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            f"pg_play:{event_id}",
                            _assistant_safe_scope_id(user_id or "guest"),
                            normalized_track_id,
                            None,
                            (event_type or "play").strip().lower() or "play",
                            "supabase_pg",
                            _recommendation_event_weight(event_type),
                            metadata_json or "{}",
                            float(created_at or time.time()),
                        ],
                    )
                    last_play_ts = max(last_play_ts, float(created_at or last_play_ts))
                    total_synced += 1
                local_connection.commit()
                if len(rows) < RECOMMENDATION_SYNC_BATCH_SIZE:
                    break

            while True:
                cursor.execute(
                    """
                    SELECT id::text, user_id::text, query, result_count,
                           EXTRACT(EPOCH FROM created_at)
                    FROM public.search_events
                    WHERE created_at > to_timestamp(%s)
                    ORDER BY created_at ASC
                    LIMIT %s
                    """,
                    [last_search_ts, RECOMMENDATION_SYNC_BATCH_SIZE],
                )
                rows = cursor.fetchall()
                if not rows:
                    break
                for row in rows:
                    search_id, user_id, query, result_count, created_at = row
                    normalized_query = _recommendation_trim_text(query)
                    if not normalized_query:
                        continue
                    local_connection.execute(
                        """
                        INSERT OR IGNORE INTO recommendation_search_events(
                            id, user_scope_id, query, result_count, source, metadata_json, occurred_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            f"pg_search:{search_id}",
                            _assistant_safe_scope_id(user_id or "guest"),
                            normalized_query,
                            max(int(result_count or 0), 0),
                            "supabase_pg",
                            "{}",
                            float(created_at or time.time()),
                        ],
                    )
                    last_search_ts = max(last_search_ts, float(created_at or last_search_ts))
                    total_synced += 1
                local_connection.commit()
                if len(rows) < RECOMMENDATION_SYNC_BATCH_SIZE:
                    break

            while True:
                cursor.execute(
                    """
                    SELECT id::text, user_id::text, track_id,
                           COALESCE(track_data::text, '{}'),
                           EXTRACT(EPOCH FROM COALESCE(updated_at, added_at))
                    FROM public.library_tracks
                    WHERE COALESCE(updated_at, added_at) > to_timestamp(%s)
                    ORDER BY COALESCE(updated_at, added_at) ASC
                    LIMIT %s
                    """,
                    [last_library_ts, RECOMMENDATION_SYNC_BATCH_SIZE],
                )
                rows = cursor.fetchall()
                if not rows:
                    break
                for row in rows:
                    library_id, user_id, track_id, metadata_json, updated_at = row
                    normalized_track_id = _recommendation_trim_text(track_id)
                    if not normalized_track_id:
                        continue
                    try:
                        payload = json.loads(metadata_json or "{}")
                    except Exception:
                        payload = {}
                    artist_name = _recommendation_trim_text(
                        payload.get("channel")
                        or payload.get("artist")
                        or payload.get("author")
                    )
                    local_connection.execute(
                        """
                        INSERT OR IGNORE INTO recommendation_events(
                            id, user_scope_id, track_id, artist_name, event_type, source, weight, metadata_json, occurred_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            f"pg_library:{library_id}",
                            _assistant_safe_scope_id(user_id or "guest"),
                            normalized_track_id,
                            artist_name or None,
                            "library",
                            "supabase_pg",
                            _recommendation_event_weight("library"),
                            metadata_json or "{}",
                            float(updated_at or time.time()),
                        ],
                    )
                    last_library_ts = max(last_library_ts, float(updated_at or last_library_ts))
                    total_synced += 1
                local_connection.commit()
                if len(rows) < RECOMMENDATION_SYNC_BATCH_SIZE:
                    break
        sync_succeeded = True
    except Exception as exc:
        _recommendation_mark_external_sync_failure(str(exc))
    finally:
        local_connection.close()
        connection.close()

    _recommendation_sync_state_set("external_play_ts", str(last_play_ts))
    _recommendation_sync_state_set("external_library_ts", str(last_library_ts))
    _recommendation_sync_state_set("external_search_ts", str(last_search_ts))
    _recommendation_sync_state_set("external_last_sync_at", str(time.time()))
    _recommendation_sync_state_set("external_last_synced_count", str(total_synced))
    if sync_succeeded:
        _recommendation_mark_external_sync_success()
    if total_synced:
        _recommendation_invalidate_collaborative_cache()
    return {
        "synced": total_synced,
        "enabled": True,
        "success": sync_succeeded,
        "sync_health": _recommendation_external_sync_health_snapshot(),
    }


@_with_server_globals
def _recommendation_model_source_signature():
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM recommendation_events) AS event_count,
                (SELECT COALESCE(MAX(occurred_at), 0) FROM recommendation_events) AS max_occurred_at,
                (SELECT COUNT(DISTINCT user_scope_id) FROM recommendation_events) AS user_count,
                (SELECT COUNT(DISTINCT track_id) FROM recommendation_events) AS item_count,
                (SELECT COUNT(*) FROM recommendation_search_events) AS search_event_count,
                (SELECT COALESCE(MAX(occurred_at), 0) FROM recommendation_search_events) AS max_search_occurred_at,
                (SELECT COUNT(DISTINCT user_scope_id) FROM recommendation_search_events) AS search_user_count,
                (SELECT COUNT(DISTINCT query) FROM recommendation_search_events) AS distinct_query_count
            """
        ).fetchone()
        payload = {
            "event_count": int(row["event_count"] or 0),
            "max_occurred_at": round(float(row["max_occurred_at"] or 0), 3),
            "user_count": int(row["user_count"] or 0),
            "item_count": int(row["item_count"] or 0),
            "search_event_count": int(row["search_event_count"] or 0),
            "max_search_occurred_at": round(float(row["max_search_occurred_at"] or 0), 3),
            "search_user_count": int(row["search_user_count"] or 0),
            "distinct_query_count": int(row["distinct_query_count"] or 0),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    finally:
        connection.close()


@_with_server_globals
def _recommendation_seeded_vector(namespace: str, key: str, dimension: int):
    values = []
    for index in range(dimension):
        digest = hashlib.sha256(f"{namespace}:{key}:{index}".encode("utf-8")).digest()
        ratio = int.from_bytes(digest[:4], "big") / 4294967295.0
        values.append((ratio - 0.5) * 0.12)
    return values


@_with_server_globals
def _recommendation_vector_dot(left: Optional[List[float]], right: Optional[List[float]]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(float(a) * float(b) for a, b in zip(left, right))


@_with_server_globals
def _recommendation_sigmoid(value: float) -> float:
    bounded = max(min(float(value), 18.0), -18.0)
    return 1.0 / (1.0 + math.exp(-bounded))


@_with_server_globals
def _recommendation_sample_negative_item(
    user_id: str,
    positive_item_id: str,
    epoch: int,
    round_index: int,
    all_items,
    positive_item_ids,
):
    if not all_items:
        return None
    digest = hashlib.sha1(
        f"{user_id}:{positive_item_id}:{epoch}:{round_index}".encode("utf-8")
    ).hexdigest()
    base_index = int(digest[:8], 16) % len(all_items)
    for offset in range(len(all_items)):
        candidate = all_items[(base_index + offset) % len(all_items)]
        if candidate not in positive_item_ids:
            return candidate
    return None


@_with_server_globals
def _recommendation_round_vector(values: Optional[List[float]], digits: int = 6):
    if not values:
        return []
    return [round(float(value), digits) for value in values]


@_with_server_globals
def _recommendation_train_collaborative_model(source_signature: str):
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        event_rows = connection.execute(
            """
            SELECT user_scope_id, track_id, artist_name, event_type, weight, metadata_json, occurred_at
            FROM recommendation_events
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            [RECOMMENDATION_MODEL_MAX_EVENTS],
        ).fetchall()
        search_rows = connection.execute(
            """
            SELECT user_scope_id, query, result_count, metadata_json, occurred_at
            FROM recommendation_search_events
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            [RECOMMENDATION_MODEL_MAX_EVENTS],
        ).fetchall()
    finally:
        connection.close()

    event_count = len(event_rows)
    if event_count < RECOMMENDATION_MODEL_MIN_EVENTS:
        return {
            "ready": False,
            "reason": "insufficient_events",
            "event_count": event_count,
            "trained_at": time.time(),
            "source_signature": source_signature,
        }

    now = time.time()
    user_track_weights = defaultdict(dict)
    user_positive_order = defaultdict(list)
    track_artists = {}
    item_popularity = defaultdict(float)
    user_search_weights = defaultdict(dict)
    query_track_scores = defaultdict(dict)
    query_artist_scores = defaultdict(dict)

    for row in event_rows:
        user_scope_id = _assistant_safe_scope_id(row["user_scope_id"] or "guest")
        track_id = _recommendation_trim_text(row["track_id"])
        if not track_id:
            continue
        event_type = (row["event_type"] or "play").strip().lower() or "play"
        base_weight = float(row["weight"] or _recommendation_event_weight(event_type))
        occurred_at = float(row["occurred_at"] or now)
        age_days = max(0.0, (now - occurred_at) / 86400.0)
        recency_weight = max(0.35, 1.0 / (1.0 + (age_days / 45.0)))
        weighted_value = base_weight * recency_weight

        try:
            payload = json.loads(row["metadata_json"] or "{}")
        except Exception:
            payload = {}
        artist_name = _recommendation_trim_text(
            row["artist_name"]
            or payload.get("channel")
            or payload.get("artist")
            or payload.get("author")
        )
        if artist_name:
            track_artists[track_id] = artist_name

        user_values = user_track_weights[user_scope_id]
        user_values[track_id] = user_values.get(track_id, 0.0) + weighted_value
        if weighted_value > 0:
            item_popularity[track_id] += weighted_value
            positives = user_positive_order[user_scope_id]
            if track_id not in positives:
                positives.append(track_id)

    for row in search_rows:
        user_scope_id = _assistant_safe_scope_id(row["user_scope_id"] or "guest")
        query = _recommendation_trim_text(row["query"])
        if not query:
            continue
        occurred_at = float(row["occurred_at"] or now)
        age_days = max(0.0, (now - occurred_at) / 86400.0)
        recency_weight = max(0.3, 1.0 / (1.0 + (age_days / 30.0)))
        base_weight = 1.0 + min(max(int(row["result_count"] or 0), 0), 12) * 0.04
        normalized_query = _normalize_text(query)
        user_search_weights[user_scope_id][normalized_query] = (
            user_search_weights[user_scope_id].get(normalized_query, 0.0)
            + (base_weight * recency_weight)
        )

    positive_user_items = {}
    holdout_track_by_user = {}
    for user_scope_id, weights in user_track_weights.items():
        positives = {
            track_id: score
            for track_id, score in weights.items()
            if score > 0.2
        }
        positive_order = [
            track_id for track_id in user_positive_order.get(user_scope_id, [])
            if positives.get(track_id, 0.0) > 0.2
        ]
        if len(positive_order) >= 3:
            holdout_track_by_user[user_scope_id] = positive_order[0]
            positives.pop(positive_order[0], None)
        if positives:
            positive_user_items[user_scope_id] = dict(
                sorted(
                    positives.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )[:80]
            )

    all_items = [
        track_id
        for track_id, _score in sorted(
            item_popularity.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ]
    if len(all_items) < 2 or not positive_user_items:
        return {
            "ready": False,
            "reason": "insufficient_positive_matrix",
            "event_count": event_count,
            "trained_at": time.time(),
            "source_signature": source_signature,
        }

    dimension = RECOMMENDATION_MODEL_FACTOR_DIM
    item_factors = {
        track_id: _recommendation_seeded_vector("item", track_id, dimension)
        for track_id in all_items
    }
    user_factors = {}
    for user_scope_id, positives in positive_user_items.items():
        seeded = _recommendation_seeded_vector("user", user_scope_id, dimension)
        blended = _vector_weighted_average(
            [
                (item_factors[track_id], float(weight))
                for track_id, weight in positives.items()
                if track_id in item_factors
            ]
        )
        user_factors[user_scope_id] = _vector_weighted_average(
            [
                (seeded, 0.35),
                (blended, 1.65),
            ]
        ) or seeded

    for epoch in range(RECOMMENDATION_MODEL_EPOCHS):
        for user_scope_id in sorted(positive_user_items.keys()):
            positives = list(positive_user_items[user_scope_id].items())[:32]
            positive_item_ids = {track_id for track_id, _weight in positives}
            user_vector = user_factors.setdefault(
                user_scope_id,
                _recommendation_seeded_vector("user", user_scope_id, dimension),
            )
            for sample_index, (positive_item_id, interaction_weight) in enumerate(positives):
                positive_vector = item_factors.get(positive_item_id)
                if positive_vector is None:
                    continue
                negative_item_id = _recommendation_sample_negative_item(
                    user_scope_id,
                    positive_item_id,
                    epoch,
                    sample_index,
                    all_items,
                    positive_item_ids,
                )
                if negative_item_id is None:
                    continue
                negative_vector = item_factors.get(negative_item_id)
                if negative_vector is None:
                    continue
                margin = (
                    _recommendation_vector_dot(user_vector, positive_vector)
                    - _recommendation_vector_dot(user_vector, negative_vector)
                )
                gradient = _recommendation_sigmoid(-margin)
                step = 0.04 * min(max(float(interaction_weight), 0.45), 3.5)
                regularization = 0.0015
                for index in range(dimension):
                    user_value = user_vector[index]
                    positive_value = positive_vector[index]
                    negative_value = negative_vector[index]
                    user_vector[index] += step * (
                        ((positive_value - negative_value) * gradient)
                        - (regularization * user_value)
                    )
                    positive_vector[index] += step * (
                        (user_value * gradient)
                        - (regularization * positive_value)
                    )
                    negative_vector[index] += step * (
                        ((-user_value) * gradient)
                        - (regularization * negative_value)
                    )
            user_factors[user_scope_id] = _vector_normalize(user_vector)

    for track_id in list(item_factors.keys()):
        item_factors[track_id] = _vector_normalize(item_factors[track_id])

    item_neighbors = {}
    co_occurrence = defaultdict(dict)
    for positives in positive_user_items.values():
        top_items = list(positives.items())[:48]
        if len(top_items) < 2:
            continue
        normalizer = math.log(2 + len(top_items))
        for index, (left_track_id, left_weight) in enumerate(top_items):
            for right_track_id, right_weight in top_items[index + 1:]:
                boost = math.sqrt(max(left_weight, 0.05) * max(right_weight, 0.05)) / normalizer
                co_occurrence[left_track_id][right_track_id] = (
                    co_occurrence[left_track_id].get(right_track_id, 0.0) + boost
                )
                co_occurrence[right_track_id][left_track_id] = (
                    co_occurrence[right_track_id].get(left_track_id, 0.0) + boost
                )

    for track_id in all_items:
        raw_neighbors = []
        for candidate_id, co_score in (co_occurrence.get(track_id) or {}).items():
            similarity = max(
                0.0,
                _assistant_cosine_similarity(
                    item_factors.get(track_id) or [],
                    item_factors.get(candidate_id) or [],
                ),
            )
            raw_neighbors.append(
                (
                    co_score + (similarity * 1.7),
                    candidate_id,
                )
            )
        raw_neighbors.sort(key=lambda item: item[0], reverse=True)
        item_neighbors[track_id] = [
            {
                "track_id": candidate_id,
                "score": round(score, 4),
            }
            for score, candidate_id in raw_neighbors[:RECOMMENDATION_MODEL_NEIGHBOR_LIMIT]
        ]

    user_query_profiles = {}
    for user_scope_id, query_weights in user_search_weights.items():
        if not query_weights:
            continue
        top_queries = sorted(
            query_weights.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:10]
        user_query_profiles[user_scope_id] = [
            {
                "query": query,
                "weight": round(float(weight), 4),
            }
            for query, weight in top_queries
        ]
        positive_tracks = positive_user_items.get(user_scope_id) or {}
        for query, query_weight in top_queries[:6]:
            for track_id, track_weight in list(positive_tracks.items())[:14]:
                boost = float(query_weight) * math.sqrt(max(float(track_weight), 0.05))
                query_track_scores[query][track_id] = (
                    query_track_scores[query].get(track_id, 0.0) + boost
                )
                artist_key = _normalize_text(track_artists.get(track_id) or "")
                if artist_key:
                    query_artist_scores[query][artist_key] = (
                        query_artist_scores[query].get(artist_key, 0.0) + boost
                    )

    for query, track_scores in list(query_track_scores.items()):
        query_track_scores[query] = {
            track_id: round(float(score), 4)
            for track_id, score in sorted(
                track_scores.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:24]
        }
    for query, artist_scores in list(query_artist_scores.items()):
        query_artist_scores[query] = {
            artist_key: round(float(score), 4)
            for artist_key, score in sorted(
                artist_scores.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:18]
        }

    evaluation_samples = 0
    hit_rate_at_10 = 0.0
    reciprocal_rank_total = 0.0
    for user_scope_id, holdout_track_id in holdout_track_by_user.items():
        user_vector = user_factors.get(user_scope_id) or []
        if not user_vector or holdout_track_id not in item_factors:
            continue
        trained_tracks = set((positive_user_items.get(user_scope_id) or {}).keys())
        ranked_candidates = []
        for candidate_id, candidate_vector in item_factors.items():
            if candidate_id in trained_tracks:
                continue
            score = max(0.0, _assistant_cosine_similarity(user_vector, candidate_vector))
            score += float(item_popularity.get(candidate_id) or 0.0) * 0.02
            ranked_candidates.append((score, candidate_id))
        ranked_candidates.sort(key=lambda item: item[0], reverse=True)
        ranking = [candidate_id for _score, candidate_id in ranked_candidates[:50]]
        if holdout_track_id not in ranking:
            continue
        evaluation_samples += 1
        rank = ranking.index(holdout_track_id) + 1
        reciprocal_rank_total += 1.0 / rank
        if rank <= 10:
            hit_rate_at_10 += 1.0

    evaluation_metrics = {
        "offline_users_evaluated": evaluation_samples,
        "hit_rate_at_10": round(
            (hit_rate_at_10 / evaluation_samples) if evaluation_samples else 0.0,
            4,
        ),
        "mrr": round(
            (reciprocal_rank_total / evaluation_samples) if evaluation_samples else 0.0,
            4,
        ),
    }

    model_id = str(uuid.uuid4())

    return {
        "ready": True,
        "model_id": model_id,
        "model_type": "implicit_bpr_collaborative",
        "trained_at": time.time(),
        "source_signature": source_signature,
        "event_count": event_count,
        "search_event_count": len(search_rows),
        "user_count": len(positive_user_items),
        "item_count": len(all_items),
        "factor_dim": dimension,
        "evaluation_metrics": evaluation_metrics,
        "item_popularity": {
            track_id: round(float(score), 4)
            for track_id, score in item_popularity.items()
        },
        "track_artists": track_artists,
        "item_factors": {
            track_id: _recommendation_round_vector(values)
            for track_id, values in item_factors.items()
        },
        "user_factors": {
            user_scope_id: _recommendation_round_vector(values)
            for user_scope_id, values in user_factors.items()
        },
        "item_neighbors": item_neighbors,
        "user_positive_tracks": {
            user_scope_id: [track_id for track_id, _weight in positives.items()]
            for user_scope_id, positives in positive_user_items.items()
        },
        "user_query_profiles": user_query_profiles,
        "query_track_scores": query_track_scores,
        "query_artist_scores": query_artist_scores,
    }


@_with_server_globals
def _recommendation_materialize_feature_store(artifact):
    if not isinstance(artifact, dict) or not artifact.get("ready"):
        return
    model_id = _recommendation_trim_text(artifact.get("model_id"))
    if not model_id:
        return
    updated_at = time.time()
    rows = []
    for user_scope_id, track_ids in (artifact.get("user_positive_tracks") or {}).items():
        rows.append(
            (
                "user_profile",
                user_scope_id,
                model_id,
                json.dumps(
                    {
                        "top_tracks": list(track_ids[:24]),
                        "top_queries": (artifact.get("user_query_profiles") or {}).get(
                            user_scope_id,
                            [],
                        ),
                    },
                    ensure_ascii=False,
                ),
                updated_at,
            )
        )
    for query, track_scores in (artifact.get("query_track_scores") or {}).items():
        rows.append(
            (
                "query_profile",
                query,
                model_id,
                json.dumps(
                    {
                        "top_tracks": track_scores,
                        "top_artists": (artifact.get("query_artist_scores") or {}).get(
                            query,
                            {},
                        ),
                    },
                    ensure_ascii=False,
                ),
                updated_at,
            )
        )
    for track_id, popularity in (artifact.get("item_popularity") or {}).items():
        rows.append(
            (
                "track_profile",
                track_id,
                model_id,
                json.dumps(
                    {
                        "artist": (artifact.get("track_artists") or {}).get(track_id) or "",
                        "popularity": popularity,
                        "neighbors": (artifact.get("item_neighbors") or {}).get(track_id, []),
                    },
                    ensure_ascii=False,
                ),
                updated_at,
            )
        )
    rows.append(
        (
            "model_metrics",
            model_id,
            model_id,
            json.dumps(artifact.get("evaluation_metrics") or {}, ensure_ascii=False),
            updated_at,
        )
    )
    _recommendation_feature_store_upsert_many(rows)


@_with_server_globals
def _recommendation_export_model_artifact(artifact):
    export_enabled = os.environ.get(
        "AURALIS_RECOMMEND_EXPORT_MODEL_JSON",
        "0",
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not export_enabled:
        return
    if not isinstance(artifact, dict) or not artifact.get("ready"):
        return
    model_id = _recommendation_trim_text(artifact.get("model_id"))
    if not model_id:
        return
    try:
        os.makedirs(RECOMMENDATION_MODEL_EXPORT_DIR, exist_ok=True)
        with open(
            os.path.join(RECOMMENDATION_MODEL_EXPORT_DIR, f"{model_id}.json"),
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(artifact, handle, ensure_ascii=False)
        retention_count = max(
            1,
            int(os.environ.get("AURALIS_RECOMMEND_MODEL_JSON_RETENTION", "10") or "10"),
        )
        json_files = []
        for filename in os.listdir(RECOMMENDATION_MODEL_EXPORT_DIR):
            if not filename.lower().endswith(".json"):
                continue
            path = os.path.join(RECOMMENDATION_MODEL_EXPORT_DIR, filename)
            try:
                json_files.append((os.path.getmtime(path), path))
            except Exception:
                continue
        json_files.sort(reverse=True)
        for _mtime, path in json_files[retention_count:]:
            try:
                os.remove(path)
            except Exception:
                continue
    except Exception:
        pass


@_with_server_globals
def _recommendation_store_collaborative_model(artifact):
    _recommendation_init_store_db()
    model_id = _recommendation_trim_text(artifact.get("model_id")) or str(uuid.uuid4())
    artifact["model_id"] = model_id
    metrics_json = json.dumps(artifact.get("evaluation_metrics") or {}, ensure_ascii=False)
    created_at = float(artifact.get("trained_at") or time.time())
    connection = _recommendation_store_connection()
    try:
        connection.execute(
            "UPDATE recommendation_model_versions SET is_active = 0"
        )
        connection.execute(
            """
            INSERT INTO recommendation_model_versions(
                id, source_signature, model_kind, artifact_json, metrics_json, created_at, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(id) DO UPDATE SET
                source_signature = excluded.source_signature,
                model_kind = excluded.model_kind,
                artifact_json = excluded.artifact_json,
                metrics_json = excluded.metrics_json,
                created_at = excluded.created_at,
                is_active = excluded.is_active
            """,
            [
                model_id,
                artifact.get("source_signature") or "",
                artifact.get("model_type") or "implicit_bpr_collaborative",
                json.dumps(artifact, ensure_ascii=False),
                metrics_json,
                created_at,
            ],
        )
        connection.execute(
            """
            INSERT INTO recommendation_models(
                id, source_signature, artifact_json, metrics_json,
                is_active, created_at, updated_at, model_kind
            )
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                source_signature = excluded.source_signature,
                artifact_json = excluded.artifact_json,
                metrics_json = excluded.metrics_json,
                is_active = excluded.is_active,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                model_kind = excluded.model_kind
            """,
            [
                "global",
                artifact.get("source_signature") or "",
                json.dumps(artifact, ensure_ascii=False),
                metrics_json,
                created_at,
                time.time(),
                artifact.get("model_type") or "implicit_bpr_collaborative",
            ],
        )
        connection.commit()
    finally:
        connection.close()
    _recommendation_materialize_feature_store(artifact)
    _recommendation_export_model_artifact(artifact)


@_with_server_globals
def _recommendation_run_maintenance_cycle(
    *,
    force_sync: bool = False,
    force_train: bool = False,
    run_experiment_evaluation: bool = False,
):
    _recommendation_init_store_db()
    cycle_started_at = time.time()
    result = {
        "synced": 0,
        "trained": False,
        "model_ready": False,
        "model_id": "",
        "source_signature": "",
        "experiment_evaluation": None,
    }
    try:
        if force_sync or bool(RECOMMENDATION_SYNC_DATABASE_DSN):
            sync_result = _recommendation_sync_external_events(force=force_sync)
            result["synced"] = int((sync_result or {}).get("synced") or 0)
            _recommendation_sync_state_set("scheduler_last_sync_at", str(time.time()))

        source_signature = _recommendation_model_source_signature()
        result["source_signature"] = source_signature
        last_trained_signature = _recommendation_sync_state_get(
            "scheduler_last_trained_signature",
            "",
        )
        last_train_at = _recommendation_sync_state_float("scheduler_last_train_at", 0.0)
        signature_changed = source_signature != last_trained_signature
        train_due = (
            force_train
            or signature_changed
            or (cycle_started_at - last_train_at) >= RECOMMENDATION_TRAIN_INTERVAL_SECONDS
        )
        if train_due:
            model = _recommendation_get_collaborative_model(
                force_refresh=True,
                force_sync=False,
            )
            result["trained"] = True
            result["model_ready"] = bool((model or {}).get("ready"))
            result["model_id"] = _recommendation_trim_text((model or {}).get("model_id"))
            _recommendation_sync_state_set("scheduler_last_train_at", str(time.time()))
            _recommendation_sync_state_set(
                "scheduler_last_trained_signature",
                source_signature,
            )
            _recommendation_sync_state_set(
                "scheduler_last_model_id",
                result["model_id"],
            )
        if RECOMMENDATION_PROMOTE_WINNER and run_experiment_evaluation:
            evaluation = _recommendation_evaluate_experiment()
            result["experiment_evaluation"] = evaluation
            if evaluation.get("evaluated"):
                _recommendation_sync_state_set(
                    "experiment_last_evaluated_at",
                    str(time.time()),
                )
                _recommendation_sync_state_set(
                    "experiment_last_evaluation_reason",
                    _recommendation_trim_text(evaluation.get("reason", "")),
                )
                if evaluation.get("promoted"):
                    _recommendation_sync_state_set(
                        "experiment_last_promoted_at",
                        str(time.time()),
                    )
        _recommendation_sync_state_set("scheduler_last_error", "")
        _recommendation_sync_state_set(
            "scheduler_last_cycle_at",
            str(time.time()),
        )
    except Exception as exc:
        _recommendation_sync_state_set("scheduler_last_error", str(exc)[:1000])
        traceback.print_exc()
    return result


@_with_server_globals
def _recommendation_bootstrap_once():
    _recommendation_run_maintenance_cycle(
        force_sync=bool(RECOMMENDATION_SYNC_DATABASE_DSN),
        force_train=True,
        run_experiment_evaluation=True,
    )


@_with_server_globals
def _recommendation_worker_heartbeat(worker_mode: str, status: str):
    _recommendation_sync_state_set("worker_mode", worker_mode)
    _recommendation_sync_state_set("worker_status", status)
    _recommendation_sync_state_set("worker_process_id", str(os.getpid()))
    _recommendation_sync_state_set("worker_last_heartbeat_at", str(time.time()))


@_with_server_globals
def _recommendation_scheduler_loop(worker_mode: str = "embedded"):
    next_sync_at = 0.0
    next_train_at = 0.0
    next_eval_at = 0.0
    minimum_sync_interval = max(30, RECOMMENDATION_SYNC_INTERVAL_SECONDS)
    minimum_train_interval = max(60, RECOMMENDATION_TRAIN_INTERVAL_SECONDS)
    minimum_eval_interval = max(60, RECOMMENDATION_EXPERIMENT_EVAL_INTERVAL_SECONDS)
    _recommendation_sync_state_set("worker_started_at", str(time.time()))
    _recommendation_worker_heartbeat(worker_mode, "running")
    while not recommendation_scheduler_stop.is_set():
        now = time.time()
        _recommendation_worker_heartbeat(worker_mode, "running")
        should_sync = now >= next_sync_at
        should_train = now >= next_train_at
        should_evaluate = RECOMMENDATION_PROMOTE_WINNER and now >= next_eval_at
        if should_sync or should_train:
            _recommendation_run_maintenance_cycle(
                force_sync=should_sync,
                force_train=should_train,
                run_experiment_evaluation=should_evaluate,
            )
            completed_at = time.time()
            if should_sync:
                next_sync_at = completed_at + minimum_sync_interval
            if should_train:
                next_train_at = completed_at + minimum_train_interval
            if should_evaluate:
                next_eval_at = completed_at + minimum_eval_interval
        elif should_evaluate:
            evaluation = _recommendation_evaluate_experiment()
            completed_at = time.time()
            next_eval_at = completed_at + minimum_eval_interval
            _recommendation_sync_state_set(
                "experiment_last_evaluated_at",
                str(completed_at),
            )
            _recommendation_sync_state_set(
                "experiment_last_evaluation_reason",
                _recommendation_trim_text(evaluation.get("reason", "")),
            )
            if evaluation.get("promoted"):
                _recommendation_sync_state_set(
                    "experiment_last_promoted_at",
                    str(completed_at),
                )
        sleep_for = max(
            5.0,
            min(
                next_sync_at - time.time(),
                next_train_at - time.time(),
                next_eval_at - time.time() if RECOMMENDATION_PROMOTE_WINNER else 30.0,
                30.0,
            ),
        )
        recommendation_scheduler_stop.wait(sleep_for)
    _recommendation_worker_heartbeat(worker_mode, "stopped")


@_with_server_globals
def _recommendation_start_scheduler():
    global recommendation_scheduler_thread
    if (
        recommendation_scheduler_thread is not None
        and recommendation_scheduler_thread.is_alive()
    ):
        return
    recommendation_scheduler_stop.clear()
    recommendation_scheduler_thread = Thread(
        target=_recommendation_scheduler_loop,
        kwargs={"worker_mode": "embedded"},
        name="recommendation-scheduler",
        daemon=True,
    )
    recommendation_scheduler_thread.start()


@_with_server_globals
def _recommendation_stop_scheduler():
    global recommendation_scheduler_thread
    recommendation_scheduler_stop.set()
    thread = recommendation_scheduler_thread
    recommendation_scheduler_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)


@_with_server_globals
def run_recommendation_worker_forever():
    recommendation_scheduler_stop.clear()
    try:
        _recommendation_scheduler_loop(worker_mode="external")
    finally:
        _recommendation_worker_heartbeat("external", "stopped")




