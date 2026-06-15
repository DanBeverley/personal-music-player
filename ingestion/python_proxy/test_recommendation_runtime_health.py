from __future__ import annotations

import pathlib
import sys
import time
import unittest

CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.recommend.runtime_health import (  # noqa: E402
    recommendation_runtime_health,
    schedule_runtime_bootstrap,
)


class RecommendationRuntimeHealthTests(unittest.TestCase):
    def test_external_worker_without_heartbeat_is_unhealthy(self) -> None:
        class Server:
            RECOMMENDATION_EXTERNAL_WORKER = True
            RECOMMENDATION_SYNC_INTERVAL_SECONDS = 300
            RECOMMENDATION_ENABLE_SCHEDULER = True

            def _recommendation_runtime_snapshot(self):
                return {
                    "external_worker_expected": True,
                    "worker_status": "running",
                    "worker_last_heartbeat_at": 0,
                }

        health = recommendation_runtime_health(Server())

        self.assertTrue(health["external_worker_expected"])
        self.assertFalse(health["worker_healthy"])
        self.assertTrue(health["external_worker_unhealthy"])
        self.assertTrue(health["scheduler_enabled"])

    def test_recent_worker_heartbeat_is_healthy(self) -> None:
        class Server:
            RECOMMENDATION_EXTERNAL_WORKER = True
            RECOMMENDATION_SYNC_INTERVAL_SECONDS = 300
            RECOMMENDATION_ENABLE_SCHEDULER = False

            def _recommendation_runtime_snapshot(self):
                return {
                    "external_worker_expected": True,
                    "worker_status": "running",
                    "worker_last_heartbeat_at": time.time(),
                }

        health = recommendation_runtime_health(Server())

        self.assertTrue(health["worker_healthy"])
        self.assertFalse(health["external_worker_unhealthy"])

    def test_bootstrap_scheduler_handles_missing_or_failing_hook(self) -> None:
        class MissingBootstrapServer:
            pass

        class FailingBootstrapServer:
            def _start_recommendation_bootstrap_thread(self):
                raise RuntimeError("scheduler unavailable")

        self.assertFalse(schedule_runtime_bootstrap(MissingBootstrapServer()))
        self.assertFalse(schedule_runtime_bootstrap(FailingBootstrapServer()))


if __name__ == "__main__":
    unittest.main()
