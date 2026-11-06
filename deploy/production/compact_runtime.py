#!/usr/bin/env python3
"""Lossless inventory maintenance; defaults to read-only aggregate audit.

Preserves all rows, immutable IDs/hashes and feature-store metadata. Writers
must be stopped for an offline full conversion; deploy compatible readers
before applying compact mode. Expanded mode is the legacy rollback path.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import gc
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ingestion" / "python_proxy"))
from auralis_backend.storage.inventory_codec import (  # noqa: E402
    MAX_LOGICAL_BYTES, _json_digest, decode_inventory_payload, encode_inventory_payload,
)

OWNERS = (
    ("candidate", "home_candidate_snapshots", None),
    ("radio", "home_radio_snapshots", None),
    ("candidate", "recommendation_feature_store", "discovery_candidate_inventory"),
    ("radio", "recommendation_feature_store", "discovery_artist_radio_inventory"),
)


def digest(value):
    return _json_digest(value, MAX_LOGICAL_BYTES + 4096)[1]


def maintain(path: Path, mode: str = "audit", *, limit: int = 0, minimum_bytes: int = 0,
             dry_run: bool = False, emit=print):
    if mode not in {"audit", "compact", "expand"}:
        raise ValueError("invalid_maintenance_mode")
    results = []
    read_only = mode == 'audit' or dry_run
    with closing(sqlite3.connect(path.resolve().as_uri() + ("?mode=ro" if read_only else "?mode=rw"), uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        if read_only:
            db.execute("PRAGMA query_only=ON")
        for kind, table, namespace in OWNERS:
            key = "entity_id" if namespace else "snapshot_id"
            where, args = (" WHERE namespace=?", (namespace,)) if namespace else ("", ())
            if minimum_bytes:
                length = 'octet_length(payload_json)' if sqlite3.sqlite_version_info >= (3,43,0) else 'length(CAST(payload_json AS BLOB))'
                where += (' AND ' if where else ' WHERE ') + length + '>=?'
                args += (minimum_bytes,)
            # Sort IDs only: never sort or fetch all large bodies simultaneously.
            ids = [r[0] for r in db.execute(f"SELECT {key} FROM {table}{where} ORDER BY {key}", args)]
            if limit:
                ids = ids[:limit]
            report = {"kind": kind, "owner": namespace or table, "records": 0, "changed": 0,
                      "literal_or_oversized": 0, "before_bytes": 0, "after_bytes": 0}
            for identity in ids:
                scope = "namespace=? AND " if namespace else ""
                params = (namespace, identity) if namespace else (identity,)
                row = db.execute(f"SELECT * FROM {table} WHERE {scope}{key}=?", params).fetchone()
                original = row["payload_json"]
                stored = json.loads(original)
                logical = decode_inventory_payload(stored, kind, copy_references=False)
                original_digest = digest(logical)
                if not namespace:
                    envelope = {"snapshot_id": row["snapshot_id"], "user_scope_id": row["user_scope_id"],
                        "profile_fingerprint": row["profile_fingerprint"], "generated_at": float(row["generated_at"]),
                        "expires_at": float(row["expires_at"]), "payload": logical}
                    if digest(envelope) != row["content_hash"]:
                        raise ValueError("immutable_content_hash_mismatch")
                    del envelope
                encoded = encode_inventory_payload(logical, kind, enabled=True) if mode == "compact" else logical
                replacement = json.dumps(encoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if digest(decode_inventory_payload(json.loads(replacement), kind, copy_references=False)) != original_digest:
                    raise ValueError("inventory_roundtrip_mismatch")
                if mode == "compact" and "_neatie_inventory_storage" not in encoded:
                    report["literal_or_oversized"] += 1
                before = len(original.encode("utf-8"))
                after = len(replacement.encode("utf-8")) if mode != "audit" else before
                report["records"] += 1
                report["before_bytes"] += before
                report["after_bytes"] += after
                if not read_only and replacement != original:
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        changed = db.execute(f"UPDATE {table} SET payload_json=? WHERE {scope}{key}=? AND payload_json=?",
                            (replacement, *params, original)).rowcount
                        if changed != 1:
                            raise ValueError("inventory_changed_during_conversion")
                        db.commit()
                    except Exception:
                        db.rollback()
                        raise
                    report["changed"] += 1
                if report["records"] % 20 == 0:
                    emit(json.dumps({"progress": report}, sort_keys=True), flush=True)
                del row, original, stored, logical, encoded, replacement
                gc.collect()
            emit(json.dumps({"owner_complete": report}, sort_keys=True), flush=True)
            results.append(report)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--mode", choices=["audit", "compact", "expand"], default="audit")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument('--minimum-bytes', type=int, default=0)
    parser.add_argument('--dry-run', action='store_true', help='Verify conversion and report sizes without writing')
    args = parser.parse_args()
    if args.mode != "audit" and not args.apply and not args.dry_run:
        parser.error("compact/expand requires --apply and compatible readers/backup")
    if args.dry_run and args.apply:
        parser.error('--dry-run and --apply are mutually exclusive')
    maintain(args.db, args.mode, limit=max(args.limit, 0), minimum_bytes=max(args.minimum_bytes,0), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
