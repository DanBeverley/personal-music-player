from __future__ import annotations

from hashlib import sha256
from typing import Any, Mapping, Sequence
import json


FEED_BUNDLE_SCHEMA = "home-feed-bundle-v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _row_id(row: Mapping[str, Any]) -> str:
    return str(row.get("id") or row.get("kind") or "").strip()


def build_feed_bundle_manifest(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Describe one fully materialized immutable artifact."""

    materialized = [dict(row) for row in rows if isinstance(row, Mapping)]
    row_ids = [_row_id(row) for row in materialized]
    complete = bool(materialized) and all(row_ids) and len(row_ids) == len(set(row_ids))
    return {
        "schema": FEED_BUNDLE_SCHEMA,
        "complete": complete,
        "all_rows_materialized": complete,
        "total_rows": len(materialized),
        "total_items": sum(
            len(row.get("items") or [])
            for row in materialized
            if isinstance(row.get("items"), list)
        ),
        "row_ids": row_ids,
        "content_hash": sha256(
            _canonical_json(materialized).encode("utf-8")
        ).hexdigest(),
    }


def with_feed_bundle_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(payload)
    rows = output.get("rows")
    materialized = [dict(row) for row in rows or [] if isinstance(row, Mapping)]
    diagnostics = output.get("diagnostics")
    output["diagnostics"] = {
        **(dict(diagnostics) if isinstance(diagnostics, Mapping) else {}),
        "feed_bundle": build_feed_bundle_manifest(materialized),
    }
    return output


def feed_bundle_manifest(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, Mapping):
        return None
    manifest = diagnostics.get("feed_bundle")
    return dict(manifest) if isinstance(manifest, Mapping) else None


def is_complete_feed_bundle_payload(payload: Mapping[str, Any]) -> bool:
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return False
    manifest = feed_bundle_manifest(payload)
    if manifest is None or manifest.get("schema") != FEED_BUNDLE_SCHEMA:
        return False
    if (
        manifest.get("complete") is not True
        or manifest.get("all_rows_materialized") is not True
    ):
        return False
    expected = build_feed_bundle_manifest(
        [dict(row) for row in rows if isinstance(row, Mapping)],
    )
    for key in (
        "schema",
        "complete",
        "all_rows_materialized",
        "total_rows",
        "total_items",
        "row_ids",
        "content_hash",
    ):
        if manifest.get(key) != expected.get(key):
            return False
    return payload.get("accepted") is True
