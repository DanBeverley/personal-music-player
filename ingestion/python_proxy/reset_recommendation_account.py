from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Iterable

CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import server
from auralis_backend.discovery.feed_state import invalidate_feed_state
from auralis_backend.recommend.feature_store import delete_taste_profile, ensure_feature_schema
from auralis_backend.recommend.session_runtime import clear_feed_sessions
from auralis_backend.recommend.store_runtime import open_recommendation_store_connection


def _delete_rows(connection, table_names: Iterable[str], user_scope_id: str) -> dict[str, int]:
    deleted_counts: dict[str, int] = {}
    for table_name in table_names:
        cursor = connection.execute(
            f"DELETE FROM {table_name} WHERE user_scope_id = ?",
            [user_scope_id],
        )
        deleted_counts[table_name] = int(cursor.rowcount or 0)
    return deleted_counts


def reset_recommendation_account(user_scope_id: str) -> dict[str, int | str]:
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    ensure_feature_schema(server)
    deleted_counts: dict[str, int] = {}
    connection = open_recommendation_store_connection(server)
    try:
        deleted_counts.update(
            _delete_rows(
                connection,
                [
                    "recommendation_events",
                    "recommendation_search_events",
                    "recommendation_impressions",
                    "recommendation_attributed_interactions",
                    "recommendation_experiment_assignments",
                    "recommendation_negative_feedback",
                    "recommendation_taste_profiles",
                ],
                normalized_scope,
            )
        )
        connection.commit()
    finally:
        connection.close()

    delete_taste_profile(server, user_scope_id=normalized_scope)
    invalidate_feed_state(server, normalized_scope)
    clear_feed_sessions(server)

    return {
        "user_scope_id": normalized_scope,
        **deleted_counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reset recommendation history and cached recommendation state for one user scope.",
    )
    parser.add_argument("user_scope_id", help="The recommendation user_scope_id to reset.")
    args = parser.parse_args()
    result = reset_recommendation_account(args.user_scope_id)
    print("Recommendation account reset complete.")
    for key, value in result.items():
        print(f"{key}: {value}")
    print("Restart the local backend after this reset for the cleanest evaluation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
