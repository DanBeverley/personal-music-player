"""Opt-in live diagnostic for the non-MusicBrainz candidate supply path.

Run explicitly:

    $env:AURALIS_RUN_CANDIDATE_CONNECTIVITY_TEST = "1"
    python -B -m pytest ingestion/python_proxy/test_candidate_provider_connectivity.py -q -s
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from auralis_backend.discovery.enrichment import (
    CandidateEnrichmentPlan,
    EnrichmentRequest,
    materialize_enrichment_plan,
)


_RUN_LIVE = (
    os.environ.get("AURALIS_RUN_CANDIDATE_CONNECTIVITY_TEST", "").strip()
    == "1"
)


@pytest.mark.skipif(
    not _RUN_LIVE,
    reason="set AURALIS_RUN_CANDIDATE_CONNECTIVITY_TEST=1 for the live diagnostic",
)
def test_listenbrainz_candidate_request_returns_results() -> None:
    request = EnrichmentRequest(
        kind="listenbrainz_sitewide_recordings",
        key="all_time",
        pool="popularity",
        relation="broad_global",
        limit=12,
        metadata={"user_scope_id": "connectivity-test", "offset": 0},
    )
    result = materialize_enrichment_plan(
        SimpleNamespace(),
        CandidateEnrichmentPlan(
            user_scope_id="connectivity-test",
            requests=[request],
        ),
        max_workers=1,
        max_pending_jobs=1,
    )

    rows = list(result.pools.get("popularity") or [])
    print(
        "[CANDIDATE-DIAG] "
        f"scheduled={result.diagnostics.get('scheduled_request_count')} "
        f"completed={result.diagnostics.get('completed_request_count')} "
        f"failed={result.diagnostics.get('failed_request_count')} "
        f"results={len(rows)}"
    )
    assert result.diagnostics.get("scheduled_request_count") == 1
    assert result.diagnostics.get("completed_request_count") == 1
    assert result.diagnostics.get("failed_request_count") == 0
    assert rows, "ListenBrainz request completed without candidate results"

