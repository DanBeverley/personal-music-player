from __future__ import annotations

import pathlib
import re
import sys
import tempfile
import unittest
from threading import Lock

CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.search.catalog_pipeline import (
    catalog_import_coverage_report,
    load_catalog_acceptance_fixtures,
)
from auralis_backend.search.intelligence import remember_catalog_entity


class _CatalogCoverageServer:
    def __init__(self, db_path: str) -> None:
        self.RECOMMENDATION_STORE_DB_PATH = db_path
        self.recommendation_store_lock = Lock()

    def _normalize_text(self, value) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())


class CatalogAcceptanceTests(unittest.TestCase):
    def test_default_acceptance_fixtures_are_bounded_and_unique(self) -> None:
        fixtures = load_catalog_acceptance_fixtures()

        self.assertGreaterEqual(len(fixtures), 75)
        self.assertLessEqual(len(fixtures), 100)
        queries = [fixture["query"].strip().lower() for fixture in fixtures]
        self.assertEqual(len(queries), len(set(queries)))
        self.assertTrue(all(fixture["expected_title"] for fixture in fixtures))

    def test_catalog_coverage_report_scores_canonical_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            server = _CatalogCoverageServer(str(pathlib.Path(tmp_dir) / "coverage.sqlite"))
            stored = remember_catalog_entity(
                server,
                user_scope_id="catalog",
                query="bring me to life",
                entity_type="track",
                item={
                    "id": "evanescence_bring_me_to_life",
                    "title": "Bring Me To Life",
                    "artist": "Evanescence",
                    "channel": "Evanescence",
                    "source": "fixture",
                },
                confidence=0.98,
                event_weight=2.0,
                event_type="fixture_seed",
            )
            self.assertTrue(stored)

            report = catalog_import_coverage_report(
                server,
                fixtures=[
                    {
                        "query": "bring me to life",
                        "expected_title": "Bring Me To Life",
                        "expected_artist": "Evanescence",
                    }
                ],
            )

            self.assertEqual(1, report["fixture_total"])
            self.assertEqual(1, report["fixture_passed"])
            self.assertEqual(1.0, report["fixture_pass_rate"])
            self.assertTrue(report["production_usable"])

    def test_catalog_coverage_report_classifies_missing_fixture_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            server = _CatalogCoverageServer(str(pathlib.Path(tmp_dir) / "coverage.sqlite"))

            report = catalog_import_coverage_report(
                server,
                fixtures=[
                    {
                        "query": "november rain",
                        "expected_title": "November Rain",
                        "expected_artist": "Guns N' Roses",
                    }
                ],
            )

            self.assertEqual(1, report["fixture_total"])
            self.assertEqual(0, report["fixture_passed"])
            self.assertEqual({"missing_resolution": 1}, report["fixture_failure_summary"])
            self.assertEqual(["november rain"], report["fixture_backfill_queries"])
            self.assertEqual("missing_resolution", report["fixture_results"][0]["failure_reason"])

if __name__ == "__main__":
    unittest.main()
