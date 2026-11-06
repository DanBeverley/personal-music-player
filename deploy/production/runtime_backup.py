#!/usr/bin/env python3
"""Create and restore consistent Neatie runtime archives.

Daily backups use SQLite's online backup API. The explicit quiescent recovery
mode locks checkpointed databases while streaming them without a staging copy.
"""

from __future__ import annotations

import argparse
from contextlib import closing, ExitStack, contextmanager
import datetime as dt
import hashlib
import gzip
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile


SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
SQLITE_SIDECAR_SUFFIXES = {"-shm", "-wal", "-journal"}
RESERVE_BYTES = 1024 * 1024 * 1024


def _is_sqlite(path: Path) -> bool:
    return path.suffix.lower() in SQLITE_SUFFIXES


def _is_sqlite_sidecar(path: Path) -> bool:
    return any(path.name.endswith(suffix) for suffix in SQLITE_SIDECAR_SUFFIXES)


def _open_source_database(source: Path, mode: str):
    # A root backup must not create root-only WAL/SHM files beside the live DB.
    # This standalone script is single-threaded; regain root for archive output.
    owner = source.stat()
    privileged = hasattr(os, 'geteuid') and os.geteuid() == 0
    if privileged:
        uid, gid = os.geteuid(), os.getegid()
        os.setegid(owner.st_gid)
        os.seteuid(owner.st_uid)
    try:
        db = sqlite3.connect(f'file:{source.as_posix()}?mode={mode}', uri=True, timeout=1 if mode == 'rw' else 30)
        try:
            db.execute('PRAGMA schema_version').fetchone()  # Open sidecars as their owner.
        except BaseException:
            db.close()
            raise
        return db
    finally:
        if privileged:
            os.seteuid(uid)
            os.setegid(gid)


def _backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(
        _open_source_database(source, 'ro')
    ) as source_db:
        with closing(sqlite3.connect(destination)) as destination_db:
            def capacity(_status, _remaining, _total):
                if shutil.disk_usage(destination.parent).free < RESERVE_BYTES:
                    raise RuntimeError('backup_staging_capacity_exhausted')
            source_db.backup(destination_db, pages=256, progress=capacity)
            result = destination_db.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"SQLite backup check failed for {source}")


def _stage_runtime(source: Path, staging: Path) -> None:
    for path in source.rglob("*"):
        if path.is_symlink():
            raise RuntimeError('backup_runtime_symlink_rejected')
        relative = path.relative_to(source)
        destination = staging / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif _is_sqlite_sidecar(path):
            continue
        elif _is_sqlite(path):
            _backup_sqlite(path, destination)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


@contextmanager
def _quiescent_runtime(source: Path):
    """Caller stops runtime writers; locks also reject a racing writer.

    Stream the actual checkpointed database, preserving rowids, blobs and all
    schema objects. This is a recovery path when a full staging copy cannot fit.
    """
    with ExitStack() as stack:
        for path in sorted(source.rglob('*')):
            if not path.is_file() or not _is_sqlite(path):
                continue
            db = stack.enter_context(closing(_open_source_database(path, 'rw')))
            result = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if result and result[0]:
                raise RuntimeError('backup_checkpoint_busy')
            db.execute('BEGIN IMMEDIATE')
            wal = Path(str(path) + '-wal')
            if wal.exists() and wal.stat().st_size > 32:
                raise RuntimeError('backup_writer_not_quiescent')
            # Full quick_check walks every data page and can stall a large
            # fragmented database for an hour. Validate the schema here; the
            # archive verification below proves every captured byte survived.
            if db.execute('PRAGMA integrity_check(sqlite_schema)').fetchone() != ('ok',):
                raise RuntimeError('backup_source_schema_failed')
        yield source


class _ReservedWriter:
    def __init__(self, stream, destination):
        self.stream, self.destination, self.check_after = stream, destination, 0

    def __getattr__(self, key):
        return getattr(self.stream, key)

    def write(self, data):
        self.check_after -= len(data)
        if self.check_after <= 0:
            if shutil.disk_usage(self.destination).free < RESERVE_BYTES:
                raise RuntimeError('backup_archive_capacity_exhausted')
            self.check_after = 16 * 1024 * 1024
        return self.stream.write(data)


def _archive_runtime(source: Path, partial: Path) -> dict[str, str]:
    expected = {}
    with partial.open('xb') as stream:
        partial.chmod(0o600)
        with tarfile.open(fileobj=_ReservedWriter(stream, partial.parent),
                          mode='w:gz', compresslevel=1, copybufsize=1024*1024) as output:
            for path in sorted(source.rglob('*')):
                if path.is_symlink():
                    raise RuntimeError('backup_runtime_symlink_rejected')
                if _is_sqlite_sidecar(path):
                    continue
                name = 'runtime/' + path.relative_to(source).as_posix()
                info = output.gettarinfo(str(path), arcname=name)
                if info.isdir():
                    output.addfile(info)
                elif info.isfile():
                    digest = hashlib.sha256()
                    class Reader:
                        def read(self, size=-1):
                            data = original.read(size)
                            digest.update(data)
                            return data
                    with path.open('rb') as original:
                        output.addfile(info, Reader())
                    expected[name] = digest.hexdigest()
    return expected


def verify_archive(archive: Path, expected: dict[str, str] | None = None) -> dict:
    """Read every archived byte; never publish a receipt for a partial archive."""
    hashes = {}
    with gzip.open(archive, 'rb') as compressed:
        with tarfile.open(fileobj=compressed, mode='r|') as source:
            for member in source:
                name = Path(member.name)
                if name.is_absolute() or '..' in name.parts or not name.parts or name.parts[0] != 'runtime':
                    raise RuntimeError('Unsafe backup member')
                if member.isdir():
                    continue
                if not member.isfile() or member.name in hashes:
                    raise RuntimeError('Unsupported or duplicate backup member')
                stream = source.extractfile(member)
                digest = hashlib.sha256()
                length = 0
                with stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(block)
                        length += len(block)
                if length != member.size:
                    raise RuntimeError('Incomplete backup member')
                hashes[member.name] = digest.hexdigest()
        # Reach gzip's trailer so truncation/CRC failures cannot look complete.
        for _ in iter(lambda: compressed.read(1024 * 1024), b''):
            pass
    if not any(name.endswith('recommendation_store.sqlite') for name in hashes):
        raise RuntimeError('Backup database is missing')
    if expected is not None and hashes != expected:
        raise RuntimeError('Backup differs from verified staging')
    with archive.open('rb') as source:
        archive_hash = hashlib.file_digest(source, 'sha256').hexdigest()
    return {'verified': True, 'archive': str(archive.resolve()), 'archive_bytes': archive.stat().st_size,
            'archive_mtime_ns': archive.stat().st_mtime_ns, 'archive_sha256': archive_hash,
            'verified_at': dt.datetime.now(dt.timezone.utc).timestamp(), 'members': hashes}


def prune_verified_backups(destination: Path, keep_count: int = 3) -> list[str]:
    """Only verified daily runtime archives are eligible; migration/rollback stay."""
    if keep_count < 3:
        raise ValueError('Keep at least three verified daily backups')
    eligible = []
    for archive in destination.glob('neatie-runtime-*.tar.gz'):
        receipt = archive.with_name(archive.name + '.verified.json')
        if archive.is_symlink() or not receipt.is_file():
            continue
        try:
            value = json.loads(receipt.read_text())
            if (value.get('verified') is True and value.get('archive') == str(archive.resolve())
                    and value.get('archive_bytes') == archive.stat().st_size
                    and value.get('archive_mtime_ns') == archive.stat().st_mtime_ns):
                eligible.append((archive, receipt))
        except (ValueError, OSError):
            continue
    removed = []
    for archive, receipt in sorted(eligible, key=lambda pair: pair[0].name, reverse=True)[keep_count:]:
        archive.unlink()
        receipt.unlink()
        removed.append(archive.name)
    return removed


def backup(source: Path, destination: Path, retention_count: int = 3, *, quiescent: bool = False) -> Path:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"Runtime directory does not exist: {source}")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = destination / f"neatie-runtime-{timestamp}.tar.gz"
    partial = archive.with_name('.' + archive.name + '.partial')
    if archive.exists() or partial.exists():
        raise FileExistsError('Backup timestamp already exists')
    try:
        with ExitStack() as stack:
            if quiescent:
                staging = stack.enter_context(_quiescent_runtime(source))
            else:
                temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix='neatie-backup-'))
                required = sum(path.stat().st_size for path in source.rglob('*')
                               if path.is_file() and not _is_sqlite_sidecar(path))
                if shutil.disk_usage(temporary).free < required + RESERVE_BYTES:
                    raise RuntimeError('backup_staging_capacity_insufficient')
                staging = Path(temporary) / 'runtime'
                staging.mkdir()
                _stage_runtime(source, staging)
            expected = _archive_runtime(staging, partial)
            proof = verify_archive(partial, expected)
            partial.replace(archive)
            proof.update(archive=str(archive), archive_mtime_ns=archive.stat().st_mtime_ns,
                         database_path=str(source / 'recommendation_store.sqlite'))
            receipt = archive.with_name(archive.name + '.verified.json')
            receipt.write_text(json.dumps(proof, sort_keys=True))
            receipt.chmod(0o600)
    finally:
        if partial.exists():
            partial.unlink()
    # Keep last known-good copies even when old. Failed attempts never trigger
    # pruning, and legacy archives without receipts require explicit review.
    prune_verified_backups(destination, retention_count)
    return archive


def _safe_extract(archive: Path, destination: Path) -> None:
    destination_root = destination.resolve()
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            if member.issym() or member.islnk() or member.isdev():
                raise RuntimeError(f"Unsupported archive member: {member.name}")
            member_path = (destination / member.name).resolve()
            if destination_root not in member_path.parents and member_path != destination_root:
                raise RuntimeError(f"Unsafe archive member: {member.name}")
        source.extractall(destination)


def _validate_staged_runtime(staging: Path) -> None:
    for database in staging.rglob("*"):
        if not database.is_file() or not _is_sqlite(database):
            continue
        with closing(
            sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        ) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"SQLite restore check failed for {database}")


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    if not hasattr(os, "chown"):
        return
    os.chown(path, uid, gid)
    for child in path.rglob("*"):
        os.chown(child, uid, gid)


def restore(archive: Path, destination: Path, uid: int, gid: int) -> Path:
    archive = archive.resolve()
    destination = destination.resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"Backup archive does not exist: {archive}")
    rollback = destination.with_name(
        f"{destination.name}.before-restore-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    with tempfile.TemporaryDirectory(prefix="neatie-restore-") as temporary:
        extracted = Path(temporary)
        _safe_extract(archive, extracted)
        staging = extracted / "runtime"
        if not staging.is_dir():
            raise RuntimeError("Backup does not contain a runtime directory")
        _validate_staged_runtime(staging)
        if destination.exists():
            destination.rename(rollback)
        try:
            shutil.copytree(staging, destination)
            _chown_tree(destination, uid, gid)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            if rollback.exists():
                rollback.rename(destination)
            raise
    return rollback


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--source", type=Path, required=True)
    backup_parser.add_argument("--destination", type=Path, required=True)
    backup_parser.add_argument("--retention-count", type=int, default=3)
    backup_parser.add_argument('--quiescent', action='store_true',
        help='Caller must stop runtime writers; stream locked checkpointed files without a staging copy')

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--archive", type=Path, required=True)
    restore_parser.add_argument("--destination", type=Path, required=True)
    restore_parser.add_argument("--uid", type=int, default=10001)
    restore_parser.add_argument("--gid", type=int, default=10001)

    args = parser.parse_args()
    if args.command == "backup":
        archive = backup(args.source, args.destination, args.retention_count,
                         quiescent=args.quiescent)
        print(archive)
    else:
        rollback = restore(args.archive, args.destination, args.uid, args.gid)
        print(rollback)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
