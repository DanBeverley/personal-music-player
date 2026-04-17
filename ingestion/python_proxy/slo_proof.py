from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple
import argparse
import json
import random
import statistics
import time

import requests


@dataclass
class ProbeResult:
    endpoint: str
    ok: bool
    latency_ms: int
    status_code: int
    payload_ok: bool
    error: str = ""


def _quantile(sorted_values: List[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    idx = max(0.0, min(1.0, q)) * (len(sorted_values) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return float(sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac)


def _latency_summary(values_ms: List[int]) -> Dict[str, float]:
    if not values_ms:
        return {
            "count": 0,
            "min_ms": 0.0,
            "max_ms": 0.0,
            "mean_ms": 0.0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
        }
    ordered = sorted(float(v) for v in values_ms)
    return {
        "count": len(values_ms),
        "min_ms": ordered[0],
        "max_ms": ordered[-1],
        "mean_ms": float(statistics.fmean(ordered)),
        "p50_ms": _quantile(ordered, 0.50),
        "p95_ms": _quantile(ordered, 0.95),
        "p99_ms": _quantile(ordered, 0.99),
    }


def _recommend_payload(user_scope_id: str) -> Dict[str, Any]:
    return {
        "query": "",
        "limit": 18,
        "surface": "home_feed",
        "user_scope_id": user_scope_id,
        "recent_queries": ["queen", "led zeppelin", "hotel california"],
        "taste_queries": ["classic rock", "70s rock"],
        "seed_id": "VixdIglCZXk",
        "recent_track_ids": ["VixdIglCZXk"],
        "top_track_ids": ["VixdIglCZXk"],
    }


def _search_payload(user_scope_id: str, query: str) -> Dict[str, Any]:
    return {
        "query": query,
        "limit": 24,
        "surface": "search",
        "user_scope_id": user_scope_id,
        "recent_queries": ["queen", "guns n roses", "eagles"],
        "taste_queries": ["classic rock", "hard rock"],
        "recent_track_ids": ["VixdIglCZXk"],
        "top_track_ids": ["VixdIglCZXk"],
    }


def _probe(
    *,
    base_url: str,
    endpoint: str,
    payload: Dict[str, Any],
    timeout_seconds: float,
) -> ProbeResult:
    start = time.perf_counter()
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}",
            json=payload,
            timeout=timeout_seconds,
        )
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return ProbeResult(
            endpoint=endpoint,
            ok=False,
            latency_ms=elapsed_ms,
            status_code=0,
            payload_ok=False,
            error=str(exc)[:240],
        )
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    payload_ok = False
    if response.status_code == 200:
        try:
            data = response.json()
        except Exception:
            data = {}
        if endpoint.endswith("/recommend"):
            rows = data.get("rows") or data.get("shelves") or []
            payload_ok = bool(rows)
        elif endpoint.endswith("/search"):
            tracks = data.get("tracks") or data.get("results") or []
            payload_ok = bool(isinstance(tracks, list))
    return ProbeResult(
        endpoint=endpoint,
        ok=(response.status_code == 200),
        latency_ms=elapsed_ms,
        status_code=int(response.status_code),
        payload_ok=payload_ok,
    )


def _run_suite(
    *,
    base_url: str,
    recommend_requests: int,
    search_requests: int,
    concurrency: int,
    timeout_seconds: float,
) -> Dict[str, Any]:
    users = [f"slo_user_{index}" for index in range(1, 9)]
    queries = [
        "queen",
        "hotel california",
        "guns n roses",
        "led zeppelin",
        "the eagles",
        "acoustic classics",
    ]
    jobs: List[Tuple[str, Dict[str, Any]]] = []
    for _ in range(max(0, recommend_requests)):
        user_scope_id = random.choice(users)
        jobs.append(("/recommend", _recommend_payload(user_scope_id)))
    for _ in range(max(0, search_requests)):
        user_scope_id = random.choice(users)
        query = random.choice(queries)
        jobs.append(("/search", _search_payload(user_scope_id, query)))
    random.shuffle(jobs)

    results: List[ProbeResult] = []
    started_at = time.time()
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        futures = [
            executor.submit(
                _probe,
                base_url=base_url,
                endpoint=endpoint,
                payload=payload,
                timeout_seconds=timeout_seconds,
            )
            for endpoint, payload in jobs
        ]
        for future in as_completed(futures):
            results.append(future.result())
    duration_ms = int((time.time() - started_at) * 1000)

    recommend_results = [result for result in results if result.endpoint == "/recommend"]
    search_results = [result for result in results if result.endpoint == "/search"]
    recommend_latency = [result.latency_ms for result in recommend_results if result.ok]
    search_latency = [result.latency_ms for result in search_results if result.ok]

    return {
        "duration_ms": duration_ms,
        "totals": {
            "requests": len(results),
            "recommend_requests": len(recommend_results),
            "search_requests": len(search_results),
            "ok": sum(1 for result in results if result.ok),
            "errors": sum(1 for result in results if not result.ok),
        },
        "recommend": {
            "summary": _latency_summary(recommend_latency),
            "ok": sum(1 for result in recommend_results if result.ok),
            "payload_ok": sum(1 for result in recommend_results if result.payload_ok),
            "errors": [
                {
                    "status_code": result.status_code,
                    "error": result.error,
                }
                for result in recommend_results
                if not result.ok
            ][:16],
        },
        "search": {
            "summary": _latency_summary(search_latency),
            "ok": sum(1 for result in search_results if result.ok),
            "payload_ok": sum(1 for result in search_results if result.payload_ok),
            "errors": [
                {
                    "status_code": result.status_code,
                    "error": result.error,
                }
                for result in search_results
                if not result.ok
            ][:16],
        },
    }


def _evaluate_targets(
    *,
    report: Dict[str, Any],
    home_p95_seconds: float,
    search_p95_seconds: float,
    p99_seconds: float,
) -> Dict[str, Any]:
    recommend_summary = dict((report.get("recommend") or {}).get("summary") or {})
    search_summary = dict((report.get("search") or {}).get("summary") or {})
    recommend_total = int((report.get("totals") or {}).get("recommend_requests") or 0)
    search_total = int((report.get("totals") or {}).get("search_requests") or 0)
    recommend_ok = int((report.get("recommend") or {}).get("ok") or 0)
    search_ok = int((report.get("search") or {}).get("ok") or 0)
    recommend_payload_ok = int((report.get("recommend") or {}).get("payload_ok") or 0)
    search_payload_ok = int((report.get("search") or {}).get("payload_ok") or 0)
    home_p95_ms = float(recommend_summary.get("p95_ms") or 0.0)
    search_p95_ms = float(search_summary.get("p95_ms") or 0.0)
    home_p99_ms = float(recommend_summary.get("p99_ms") or 0.0)
    search_p99_ms = float(search_summary.get("p99_ms") or 0.0)
    min_recommend_ok = max(1, int(recommend_total * 0.9))
    min_search_ok = max(1, int(search_total * 0.9))
    checks = {
        "recommend_success_rate": recommend_ok >= min_recommend_ok,
        "search_success_rate": search_ok >= min_search_ok,
        "recommend_payload_fill": recommend_payload_ok >= max(1, int(recommend_total * 0.7)),
        "search_payload_fill": search_payload_ok >= max(1, int(search_total * 0.7)),
        "home_p95": home_p95_ms <= (home_p95_seconds * 1000.0),
        "search_p95": search_p95_ms <= (search_p95_seconds * 1000.0),
        "home_p99": home_p99_ms <= (p99_seconds * 1000.0),
        "search_p99": search_p99_ms <= (p99_seconds * 1000.0),
    }
    return {
        "targets": {
            "home_p95_ms": home_p95_seconds * 1000.0,
            "search_p95_ms": search_p95_seconds * 1000.0,
            "p99_ms": p99_seconds * 1000.0,
            "recommend_min_ok": min_recommend_ok,
            "search_min_ok": min_search_ok,
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Auralis SLO load proof harness")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Backend base URL")
    parser.add_argument("--recommend-requests", type=int, default=60)
    parser.add_argument("--search-requests", type=int, default=90)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--timeout-seconds", type=float, default=35.0)
    parser.add_argument("--home-p95-seconds", type=float, default=6.0)
    parser.add_argument("--search-p95-seconds", type=float, default=3.0)
    parser.add_argument("--p99-seconds", type=float, default=10.0)
    parser.add_argument(
        "--output",
        default="runtime/slo_proof/latest.json",
        help="Path to write JSON report",
    )
    args = parser.parse_args()

    report = _run_suite(
        base_url=args.base_url,
        recommend_requests=args.recommend_requests,
        search_requests=args.search_requests,
        concurrency=args.concurrency,
        timeout_seconds=args.timeout_seconds,
    )
    evaluation = _evaluate_targets(
        report=report,
        home_p95_seconds=args.home_p95_seconds,
        search_p95_seconds=args.search_p95_seconds,
        p99_seconds=args.p99_seconds,
    )
    report["evaluation"] = evaluation
    report["generated_at"] = time.time()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote SLO proof report: {output_path}")
    print(json.dumps(evaluation, ensure_ascii=False))
    return 0 if bool(evaluation.get("passed")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
