#!/usr/bin/env python3
"""Reference-aware runtime retention. Audit by default; bounded online or offline apply.

Keeps the existing 12-generation candidate recovery window, current/ready feeds,
the fallback window, working inventory references, active jobs and model users.
This tool never imports server configuration or contacts providers.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import gc
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ingestion' / 'python_proxy'))
from auralis_backend.storage.inventory_codec import decode_inventory_payload

DAY = 86400
MAX_BATCH_BYTES = 256 * 1024**2
OWNERS = {'home_candidate_snapshots': 'snapshot_id',
          'home_radio_snapshots': 'snapshot_id', 'home_feed_artifacts': 'artifact_id',
          'recommendation_model_versions': 'id', 'home_feed_jobs': 'job_id'}


def _byte_size(column):
    # AWS uses SQLite 3.45; older local test runtimes lack octet_length.
    return (f'octet_length({column})' if sqlite3.sqlite_version_info >= (3, 43, 0)
            else f'length(CAST({column} AS BLOB))')


def _references(value, identities, output, seen=None):
    if seen is None:
        seen = set()
    if isinstance(value, (dict, list)):
        if id(value) in seen:
            return
        seen.add(id(value))
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and any(word in key for word in ('generation', 'snapshot', 'artifact', 'model')):
                if identities is None or item in identities:
                    output.add(item)
            elif isinstance(item, (dict, list)):
                _references(item, identities, output, seen)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str) and (identities is None or item in identities):
                output.add(item)
            elif isinstance(item, (dict, list)):
                _references(item, identities, output, seen)


class ReferenceCache:
    """Verified projection of unchanged bytes; never an inventory owner.

    A raw SHA and kind bind every hit to the exact stored document. Cache only
    after the existing decoder has validated it; partial work survives a WAL
    conflict so the next fresh transaction need not expand it again.
    """
    def __init__(self, path):
        self.path, self.entries, self.used, self.complete = path, {}, {}, False
        try:
            if path.stat().st_size <= 16 * 1024 * 1024:
                with gzip.open(path, 'rb') as stream:
                    raw = stream.read(64 * 1024 * 1024 + 1)
                if len(raw) <= 64 * 1024 * 1024:
                    self.entries = json.loads(raw)
            if not isinstance(self.entries, dict): self.entries = {}
        except (OSError, ValueError, EOFError): pass

    def references(self, raw, kind):
        key = kind + ':' + hashlib.sha256(raw.encode('utf-8')).hexdigest()
        entry = self.entries.get(key)
        if (not isinstance(entry, dict) or not isinstance(entry.get('refs'), list)
                or not all(isinstance(ref, str) for ref in entry['refs'])
                or entry.get('checksum') != hashlib.sha256(json.dumps(entry['refs']).encode()).hexdigest()):
            payload = decode_inventory_payload(json.loads(raw), kind, copy_references=False)
            refs = set(); _references(payload, None, refs)
            del payload
            entry = {'refs': sorted(refs)}
        if entry.get('schema') != 2:
            # Full hashes preserve membership without retaining long artwork
            # URLs. A collision can only keep an extra row, never lose a ref.
            entry = {'schema': 2, 'refs': sorted({hashlib.sha256(ref.encode()).hexdigest() for ref in entry['refs']})}
            entry['checksum'] = hashlib.sha256(json.dumps(entry['refs']).encode()).hexdigest()
        self.used[key] = entry
        return entry['refs']

    def save(self):
        # Keep current projections only. A failed/truncated file is a cache miss.
        entries = dict(list((self.used if self.complete else {**self.entries, **self.used}).items())[-512:])
        if entries == self.entries and self.path.is_file():
            return  # Repeated batches must not recompress/rewrite unchanged projections.
        raw = json.dumps(entries, separators=(',', ':')).encode()
        if len(raw) > 64 * 1024 * 1024: return
        raw = gzip.compress(raw, compresslevel=1)
        while entries and len(raw) > 16 * 1024 * 1024:
            entries.pop(next(iter(entries)))
            raw = gzip.compress(json.dumps(entries, separators=(',', ':')).encode(), compresslevel=1)
        temporary = self.path.with_suffix('.partial')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as stream: stream.write(raw)
        temporary.replace(self.path)
        print(json.dumps({'reference_cache_entries': len(entries), 'stored_bytes': len(raw)}), file=sys.stderr)


def plan(db, now, reference_cache=None):
    db.row_factory = sqlite3.Row
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    required = set(OWNERS) | {'home_feed_state', 'recommendation_feature_store'}
    if not required <= tables:
        raise ValueError('missing_retention_owner')
    records, keep = {}, set()
    for table, key in list(OWNERS.items())[:3]:
        stamp = 'created_at' if table == 'home_feed_artifacts' else 'generated_at'
        records[table] = list(db.execute(f'SELECT {key} AS id,user_scope_id,profile_fingerprint,{stamp} AS stamp,expires_at,'
            + ('candidate_snapshot_id,radio_snapshot_id,' if table == 'home_feed_artifacts' else '')
            + f' {_byte_size("payload_json")} AS bytes FROM {table} ORDER BY {stamp} DESC,{key} DESC'))
        groups = defaultdict(int)
        for row in records[table]:
            group = (row['user_scope_id'], row['profile_fingerprint'])
            # Preserve every input the existing bounded recovery scans can use.
            limit = 3 if table == 'home_feed_artifacts' else 12
            if groups[group] < limit or row['stamp'] > now - 3 * DAY or row['expires_at'] > now:
                keep.add(row['id'])
            groups[group] += 1
            if table == 'home_feed_artifacts' and row['expires_at'] > now - 3 * DAY:
                keep.add(row['id'])
    snapshot_identities = {r['id'] for rows in records.values() for r in rows}
    model_identities = {r[0] for r in db.execute('SELECT id FROM recommendation_model_versions')}
    identities = snapshot_identities | model_identities
    identity_hashes = {hashlib.sha256(identity.encode()).hexdigest(): identity for identity in identities}
    state = list(db.execute('SELECT active_artifact_id,ready_artifact_ids_json FROM home_feed_state'))
    for row in state:
        refs = [row['active_artifact_id'], *json.loads(row['ready_artifact_ids_json'])]
        if any(ref and ref not in snapshot_identities for ref in refs):
            raise ValueError('existing_state_reference_missing')
        keep.update(ref for ref in refs if ref)
    jobs = list(db.execute('SELECT job_id,status,updated_at,payload_json FROM home_feed_jobs'))
    for row in jobs:
        if row['status'] not in {'succeeded', 'cancelled', 'failed'}:
            _references(json.loads(row['payload_json']), identities, keep)
    # Decode one current working inventory at a time, using the existing codec.
    # Never prune acquisition jobs or their mutable recovery progress here.
    for row in db.execute("SELECT namespace,payload_json FROM recommendation_feature_store WHERE namespace IN ('discovery_candidate_inventory','discovery_artist_radio_inventory')"):
        kind = 'candidate' if row['namespace'] == 'discovery_candidate_inventory' else 'radio'
        if reference_cache is not None:
            keep.update(identity_hashes[key] for key in set(reference_cache.references(row['payload_json'], kind)) & identity_hashes.keys())
        else:
            payload = decode_inventory_payload(json.loads(row['payload_json']), kind)
            _references(payload, identities, keep)
            del payload
            gc.collect()
    # Every retained feed owns the immutable candidate/radio records it uses.
    for row in records['home_feed_artifacts']:
        if row['id'] in keep:
            for key in ('candidate_snapshot_id', 'radio_snapshot_id'):
                if row[key] and row[key] not in identities:
                    raise ValueError('existing_artifact_reference_missing')
                if row[key]:
                    keep.add(row[key])
    deletions = {table: [r['id'] for r in rows if r['id'] not in keep]
                 for table, rows in records.items()}
    model_keep = keep & model_identities
    models = list(db.execute(f'SELECT id,source_signature,model_kind,created_at,is_active,{_byte_size("artifact_json")} AS bytes FROM recommendation_model_versions ORDER BY created_at DESC,id DESC'))
    groups = defaultdict(int)
    for row in models:
        if row['is_active'] or groups[row['model_kind']] < 24 or float(row['created_at'] or 0) > now - 7 * DAY:
            model_keep.add(row['id'])
        groups[row['model_kind']] += 1
    for table in ('recommendation_impressions', 'recommendation_attributed_interactions'):
        if table in tables:
            model_keep.update(r[0] for r in db.execute(f'SELECT DISTINCT model_id FROM {table} WHERE model_id IS NOT NULL'))
    model_keep.update(r[0] for r in db.execute("SELECT DISTINCT model_id FROM recommendation_feature_store WHERE namespace != 'model_metrics'"))
    if 'recommendation_models' in tables:
        signatures = {r[0] for r in db.execute('SELECT source_signature FROM recommendation_models')}
        model_keep.update(r['id'] for r in models if r['source_signature'] in signatures)
    deletions['recommendation_model_versions'] = [r['id'] for r in models if r['id'] not in model_keep]
    deletions['home_feed_jobs'] = [r['job_id'] for r in jobs if r['status'] in {'succeeded','cancelled','failed'} and r['updated_at'] < now - 7 * DAY]
    summary = {}
    records['recommendation_model_versions'] = models
    records['home_feed_jobs'] = []
    for table, ids in deletions.items():
        chosen = set(ids)
        summary[table] = {'remove': len(ids), 'total': len(jobs) if table == 'home_feed_jobs' else len(records[table]),
                         'payload_bytes': sum(r['bytes'] or 0 for r in records[table] if r['id'] in chosen)}
    return deletions, summary


def maintain(path, *, apply=False, now=None, online=False, max_rows=500, budget_seconds=60,
             max_payload_bytes=MAX_BATCH_BYTES):
    uri = Path(path).resolve().as_uri() + ('?mode=rw' if apply else '?mode=ro')
    db = sqlite3.connect(uri, uri=True, timeout=0.25 if online else 5)
    cache = ReferenceCache(Path(path).with_name('retention-reference-cache.json.gz')) if apply else None
    try:
        deadline = time.monotonic() + budget_seconds
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
        db.execute('PRAGMA temp_store=MEMORY')
        if apply:
            # FAST clears content in pages already being written without
            # rewriting gigabytes of overflow pages just before compaction.
            # It is connection-local; the application keeps its own setting.
            db.execute('PRAGMA secure_delete=FAST')
        if online and db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
            raise ValueError('online_retention_requires_wal')
        if not apply:
            db.execute('PRAGMA query_only=ON')
        # A WAL read transaction cannot upgrade after another writer commits:
        # SQLITE_BUSY_SNAPSHOT aborts the whole attempt, preserving new references.
        # Never detach the plan from its transaction or retry its stale deletions.
        db.execute('BEGIN IMMEDIATE' if apply and not online else 'BEGIN')
        deletions, summary = plan(db, time.time() if now is None else now, cache) if cache else plan(db, time.time() if now is None else now)
        if apply:
            print(json.dumps({'retention_phase':'planned','seconds':round(time.monotonic()-(deadline-budget_seconds),2)}),file=sys.stderr,flush=True)
        if cache is not None: cache.complete = True
        if time.monotonic() >= deadline:
            raise TimeoutError('retention_budget_exceeded')
        if apply:
            remaining_bytes = max_payload_bytes
            # Delete dependants before immutable inputs; retain all other tables.
            for table in ('home_feed_artifacts','home_candidate_snapshots','home_radio_snapshots','recommendation_model_versions','home_feed_jobs'):
                chosen = []
                payload = 'artifact_json' if table == 'recommendation_model_versions' else 'payload_json'
                for identity in deletions[table][:max_rows]:
                    row = db.execute(f'SELECT {_byte_size(payload)} FROM {table} WHERE {OWNERS[table]}=?', (identity,)).fetchone()
                    size = int(row[0] or 0) if row else 0
                    if size > max_payload_bytes:
                        raise ValueError('retention_row_exceeds_byte_budget')
                    if size > remaining_bytes:
                        break
                    chosen.append(identity)
                    remaining_bytes -= size
                guard = ''
                if table in ('home_candidate_snapshots', 'home_radio_snapshots'):
                    column = 'candidate_snapshot_id' if table == 'home_candidate_snapshots' else 'radio_snapshot_id'
                    # A bounded artifact batch may leave planned dependants for
                    # the next run. Their inputs must survive this run too.
                    guard = f' AND NOT EXISTS (SELECT 1 FROM home_feed_artifacts WHERE {column}={table}.snapshot_id)'
                before = db.total_changes
                print(json.dumps({'retention_phase':'delete','table':table,'rows':len(chosen)}),file=sys.stderr,flush=True)
                db.executemany(f'DELETE FROM {table} WHERE {OWNERS[table]}=?{guard}', ((i,) for i in chosen))
                summary[table]['removed'] = db.total_changes - before
            if time.monotonic() >= deadline:
                raise TimeoutError('retention_budget_exceeded')
            db.commit()
            print(json.dumps({'retention_phase':'committed'}),file=sys.stderr,flush=True)
        else:
            db.rollback()
        return summary
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
        if cache is not None:
            try: cache.save()
            except OSError as exc:
                print(json.dumps({'reference_cache_save_errno': exc.errno}), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--writers-stopped', action='store_true')
    parser.add_argument('--online', action='store_true')
    parser.add_argument('--max-rows', type=int, default=500)
    parser.add_argument('--budget-seconds', type=int, default=60)
    parser.add_argument('--max-payload-bytes', type=int, default=MAX_BATCH_BYTES)
    parser.add_argument('--verified-backup', type=Path)
    args = parser.parse_args()
    if args.apply:
        if (not args.writers_stopped and not args.online) or not args.verified_backup:
            parser.error('apply requires stopped writers or WAL online mode, and a verified backup receipt')
        receipt = json.loads(args.verified_backup.read_text())
        if (receipt.get('verified') is not True or receipt.get('database_path') != str(args.db.resolve())
                or time.time() - float(receipt.get('verified_at', 0)) > DAY):
            parser.error('backup receipt is missing, stale or belongs to another database')
        archive = Path(receipt.get('archive', ''))
        if (not archive.is_file() or archive.stat().st_size != receipt.get('archive_bytes')
                or archive.stat().st_mtime_ns != receipt.get('archive_mtime_ns')):
            parser.error('verified backup is absent or has changed')
    if not 1 <= args.max_rows <= 5000 or not 1 <= args.budget_seconds <= 300:
        parser.error('invalid maintenance bounds')
    if not 1 <= args.max_payload_bytes <= 2 * 1024**3:
        parser.error('invalid maintenance byte budget')
    # The root service can validate protected backup receipts, but SQLite
    # writes must use its owner: a new root-owned 0600 WAL would block the API.
    if args.apply and os.name == 'posix' and os.geteuid() == 0:
        owner = args.db.stat()
        os.setgroups([])
        os.setgid(owner.st_gid)
        os.setuid(owner.st_uid)
    print(json.dumps({'applied': args.apply, 'tables': maintain(args.db, apply=args.apply,
        online=args.online, max_rows=args.max_rows, budget_seconds=args.budget_seconds,
        max_payload_bytes=args.max_payload_bytes)}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
