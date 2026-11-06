from __future__ import annotations

import argparse
import json
import pathlib
import sys
from threading import Lock
from types import SimpleNamespace

CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.search.catalog_pipeline import (  # noqa: E402
    catalog_import_coverage_report,
    load_catalog_acceptance_fixtures,
)


def _server_for_db(db_path: str):
    return SimpleNamespace(
        RECOMMENDATION_STORE_DB_PATH=db_path,
        recommendation_store_lock=Lock(),
    )


def _print_summary(report: dict) -> None:
    queue = report.get("queue_by_status") or {}
    entities = report.get("catalog_entities") or {}
    print("Catalog coverage")
    print(f"  Production usable: {bool(report.get('production_usable'))}")
    print(
        "  Fixtures: "
        f"{int(report.get('fixture_passed') or 0)}/"
        f"{int(report.get('fixture_total') or 0)} "
        f"({float(report.get('fixture_pass_rate') or 0.0) * 100:.1f}%)"
    )
    print(f"  Catalog entities: {int(report.get('catalog_total') or 0)} {entities}")
    print(f"  Aliases: {int(report.get('alias_total') or 0)}")
    print(f"  Sources: {int(report.get('source_total') or 0)}")
    print(f"  Playable sources: {int(report.get('playable_source_total') or 0)}")
    print(f"  Learned entities: {int(report.get('learned_entity_total') or 0)}")
    print(f"  Import queue: {int(report.get('queue_total') or 0)} {queue}")
    failure_summary = report.get("fixture_failure_summary") or {}
    if failure_summary:
        print(f"  Fixture failure summary: {failure_summary}")
    backfill_queries = list(report.get("fixture_backfill_queries") or [])
    if backfill_queries:
        print("  Suggested backfill queries:")
        print("   " + ", ".join(str(query) for query in backfill_queries[:12]))
    failed = [
        item
        for item in report.get("fixture_results") or []
        if not item.get("passed")
    ]
    if failed:
        print("  Fixture failures:")
        for item in failed[:20]:
            print(
                "   - "
                f"[{item.get('failure_reason') or 'failed'}] "
                f"{item.get('query')}: expected "
                f"{item.get('expected_title')} / {item.get('expected_artist')}, "
                f"got {item.get('resolved_title') or '-'} / "
                f"{item.get('resolved_artist') or '-'}"
            )
        if len(failed) > 20:
            print(f"   ... {len(failed) - 20} more")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print Neatie catalog import coverage and search fixture readiness."
    )
    parser.add_argument(
        "--db-path",
        default="runtime/recommendation_store.sqlite",
        help="Recommendation/catalog SQLite DB path.",
    )
    parser.add_argument(
        "--fixtures",
        default=str(CURRENT_DIR / "catalog_acceptance_fixtures.json"),
        help="Catalog acceptance fixture JSON path.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full report as JSON.",
    )
    args = parser.parse_args()

    fixtures = load_catalog_acceptance_fixtures(args.fixtures)
    report = catalog_import_coverage_report(
        _server_for_db(args.db_path),
        fixtures=fixtures,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
