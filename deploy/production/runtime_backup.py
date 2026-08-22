#!/usr/bin/env python3
"""Create and restore consistent Neatie runtime archives.

SQLite databases are copied with SQLite's online backup API instead of a raw
filesystem copy, so the daily backup remains valid while the API and worker are
running.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import datetime as dt
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile


SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
SQLITE_SIDECAR_SUFFIXES = {"-shm", "-wal", "-journal"}


def _is_sqlite(path: Path) -> bool:
    return path.suffix.lower() in SQLITE_SUFFIXES


def _is_sqlite_sidecar(path: Path) -> bool:
    return any(path.name.endswith(suffix) for suffix in SQLITE_SIDECAR_SUFFIXES)


def _backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(
        sqlite3.connect(
            f"file:{source.as_posix()}?mode=ro",
            uri=True,
            timeout=30,
        )
    ) as source_db:
        with closing(sqlite3.connect(destination)) as destination_db:
            source_db.backup(destination_db)
            result = destination_db.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"SQLite backup check failed for {source}")


def _stage_runtime(source: Path, staging: Path) -> None:
    for path in source.rglob("*"):
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


def backup(source: Path, destination: Path, retention_days: int) -> Path:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"Runtime directory does not exist: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = destination / f"neatie-runtime-{timestamp}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="neatie-backup-") as temporary:
        staging = Path(temporary) / "runtime"
        staging.mkdir()
        _stage_runtime(source, staging)
        with tarfile.open(archive, "w:gz") as output:
            output.add(staging, arcname="runtime", recursive=True)

    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - retention_days * 86400
    for old_archive in destination.glob("neatie-runtime-*.tar.gz"):
        if old_archive != archive and old_archive.stat().st_mtime < cutoff:
            old_archive.unlink()
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
    backup_parser.add_argument("--retention-days", type=int, default=14)

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--archive", type=Path, required=True)
    restore_parser.add_argument("--destination", type=Path, required=True)
    restore_parser.add_argument("--uid", type=int, default=10001)
    restore_parser.add_argument("--gid", type=int, default=10001)

    args = parser.parse_args()
    if args.command == "backup":
        archive = backup(args.source, args.destination, args.retention_days)
        print(archive)
    else:
        rollback = restore(args.archive, args.destination, args.uid, args.gid)
        print(rollback)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
