from __future__ import annotations

import argparse
import time

from auralis_backend.domain.ranking import (
    HOME_BECAUSE_PLAYED_DEFAULT_WEIGHTS,
    HOME_CONTINUE_DEFAULT_WEIGHTS,
    HOME_DISCOVERY_DEFAULT_WEIGHTS,
    HOME_GLOBAL_DEFAULT_WEIGHTS,
    HOME_QUIET_DEFAULT_WEIGHTS,
    HOME_TRENDING_DEFAULT_WEIGHTS,
    SEARCH_ALBUM_DEFAULT_WEIGHTS,
    SEARCH_ARTIST_DEFAULT_WEIGHTS,
    SEARCH_TRACK_DEFAULT_WEIGHTS,
)
from auralis_backend.legacy import get_server
from auralis_backend.recommend.allocator import (
    ROW_ALLOCATOR_DEFAULTS_BY_KEY,
    ROW_ALLOCATOR_MODEL_KEYS,
)
from auralis_backend.storage.object_store import get_object_store
from auralis_backend.storage.postgres import (
    ensure_backend_schema,
    upsert_default_model_weights,
    write_metric,
)


def _register_linear_models(model_id: str) -> None:
    upsert_default_model_weights(
        model_key="search_track_reranker_v2",
        version=model_id,
        model_type="linear_reranker",
        weights=SEARCH_TRACK_DEFAULT_WEIGHTS,
        metadata={"surface": "search", "entity_type": "track"},
    )
    upsert_default_model_weights(
        model_key="search_artist_reranker_v2",
        version=model_id,
        model_type="linear_reranker",
        weights=SEARCH_ARTIST_DEFAULT_WEIGHTS,
        metadata={"surface": "search", "entity_type": "artist"},
    )
    upsert_default_model_weights(
        model_key="search_album_reranker_v2",
        version=model_id,
        model_type="linear_reranker",
        weights=SEARCH_ALBUM_DEFAULT_WEIGHTS,
        metadata={"surface": "search", "entity_type": "album"},
    )
    upsert_default_model_weights(
        model_key="recommendation_home_reranker_v2",
        version=model_id,
        model_type="linear_reranker",
        weights=HOME_GLOBAL_DEFAULT_WEIGHTS,
        metadata={"surface": "home_feed", "entity_type": "track"},
    )
    upsert_default_model_weights(
        model_key="home_global_ranker_v4",
        version=model_id,
        model_type="linear_reranker",
        weights=HOME_GLOBAL_DEFAULT_WEIGHTS,
        metadata={"surface": "home_feed", "entity_type": "track", "row_kind": "all"},
    )
    upsert_default_model_weights(
        model_key="home_continue_ranker_v1",
        version=model_id,
        model_type="linear_reranker",
        weights=HOME_CONTINUE_DEFAULT_WEIGHTS,
        metadata={"surface": "home_feed", "entity_type": "track", "row_kind": "continue_listening"},
    )
    upsert_default_model_weights(
        model_key="home_because_played_ranker_v1",
        version=model_id,
        model_type="linear_reranker",
        weights=HOME_BECAUSE_PLAYED_DEFAULT_WEIGHTS,
        metadata={"surface": "home_feed", "entity_type": "track", "row_kind": "because_you_played"},
    )
    upsert_default_model_weights(
        model_key="home_quiet_ranker_v1",
        version=model_id,
        model_type="linear_reranker",
        weights=HOME_QUIET_DEFAULT_WEIGHTS,
        metadata={"surface": "home_feed", "entity_type": "track", "row_kind": "quiet_picks"},
    )
    upsert_default_model_weights(
        model_key="home_trending_ranker_v1",
        version=model_id,
        model_type="linear_reranker",
        weights=HOME_TRENDING_DEFAULT_WEIGHTS,
        metadata={"surface": "home_feed", "entity_type": "track", "row_kind": "trending_for_you"},
    )
    upsert_default_model_weights(
        model_key="home_discovery_ranker_v1",
        version=model_id,
        model_type="linear_reranker",
        weights=HOME_DISCOVERY_DEFAULT_WEIGHTS,
        metadata={"surface": "home_feed", "entity_type": "track", "row_kind": "discovery"},
    )
    allocator_row_kinds = {
        model_key: row_kind
        for row_kind, model_key in ROW_ALLOCATOR_MODEL_KEYS.items()
    }
    for model_key, weights in ROW_ALLOCATOR_DEFAULTS_BY_KEY.items():
        upsert_default_model_weights(
            model_key=model_key,
            version=model_id,
            model_type="linear_allocator",
            weights=weights,
            metadata={
                "surface": "home_feed",
                "entity_type": "row_allocator",
                "row_kind": allocator_row_kinds.get(model_key) or "",
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Train and export Auralis recommender artifacts")
    parser.add_argument("--force-sync", action="store_true", help="Sync external events before training")
    parser.add_argument("--skip-eval", action="store_true", help="Skip experiment evaluation")
    args = parser.parse_args()

    ensure_backend_schema()
    server = get_server()
    if args.force_sync:
        server._recommendation_sync_external_events(force=True)
    model = server._recommendation_get_collaborative_model(force_refresh=True)
    if not args.skip_eval:
        server._recommendation_evaluate_experiment(
            force_promote=bool(server.RECOMMENDATION_PROMOTE_WINNER),
            window_hours=server.RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS,
        )
    model_id = (model or {}).get("model_id") or f"bootstrap-{int(time.time())}"
    _register_linear_models(model_id)
    metrics = {
        "event_count": float((model or {}).get("event_count") or 0),
        "search_event_count": float((model or {}).get("search_event_count") or 0),
        "user_count": float((model or {}).get("user_count") or 0),
        "item_count": float((model or {}).get("item_count") or 0),
    }
    for metric_name, metric_value in metrics.items():
        write_metric(
            model_key="recommendation_home_reranker_v2",
            version=model_id,
            metric_name=metric_name,
            metric_value=metric_value,
            metadata={"source": "trainer_main"},
        )
    artifact_payload = {
        "generated_at": time.time(),
        "model": model or {},
        "registered_models": [
            "search_track_reranker_v2",
            "search_artist_reranker_v2",
            "search_album_reranker_v2",
            "recommendation_home_reranker_v2",
            "home_global_ranker_v4",
            "home_continue_ranker_v1",
            "home_because_played_ranker_v1",
            "home_quiet_ranker_v1",
            "home_trending_ranker_v1",
            "home_discovery_ranker_v1",
            *sorted(ROW_ALLOCATOR_DEFAULTS_BY_KEY.keys()),
        ],
        "metrics": metrics,
    }
    artifact_uri = get_object_store().write_json(
        f"models/{model_id}/training_snapshot.json",
        artifact_payload,
    )
    print("Auralis Trainer")
    print(f"model_id={model_id}")
    print(f"artifact={artifact_uri}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
