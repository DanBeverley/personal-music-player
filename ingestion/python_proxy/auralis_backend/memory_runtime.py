"""Opt-in, account-free process/cgroup telemetry; no payload or heap inspection."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import re
from pathlib import Path
import threading
import time
import uuid

_lock = threading.Lock()
_active: dict[str, dict[str, str]] = {}
_monitor = None
_stop = threading.Event()
_last_tick = 0.0
_OPERATIONS = {"inventory", "extractor", "solver"}


def _enabled():
    return os.environ.get("NEATIE_MEMORY_DIAGNOSTICS", "0") == "1"


def _integer(path):
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def memory_sample():
    """Bounded /proc reads, including children spawned by worker threads."""
    result = {}
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"VmRSS", "VmHWM", "RssAnon"}:
                result[key + "Bytes"] = int(value.split()[0]) * 1024
        children = set()
        for task in list(Path("/proc/self/task").iterdir())[:128]:
            try:
                children.update((task / "children").read_text().split())
            except OSError:
                pass
        result["childCount"] = len(children)
        result["childRssBytes"] = 0
        for child in sorted(children)[:32]:
            try:
                for line in Path("/proc", child, "status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        result["childRssBytes"] += int(line.split()[1]) * 1024
            except OSError:
                pass  # Children can finish between the listing and this read.
        # Cgroup namespaces expose the container root here. Host execution may
        # expose a host root; only advertise a finite limit if one exists.
        root = Path("/sys/fs/cgroup")
        for label, filename in (("cgroupBytes", "memory.current"), ("cgroupPeakBytes", "memory.peak"),
                                ("cgroupLimitBytes", "memory.max")):
            number = _integer(root / filename)
            if number is not None:
                result[label] = number
    except (OSError, ValueError):
        pass  # Diagnostics must never change serving behavior.
    return result


def _emit(event, **facts):
    try:
        from .storage.cache_runtime import metadata_cache_stats
        print("[EBB:memory] " + json.dumps({"event": event, **facts,
            **memory_sample(), **metadata_cache_stats()}, separators=(",", ":")), flush=True)
    except Exception:
        pass


def _run():
    while not _stop.wait(1):
        with _lock:
            active = dict(_active)
        if active:
            _emit("active", operations=active)


def memory_tick():
    """Use the existing maintenance cadence; at most one daemon per process."""
    if not _enabled():
        return
    global _monitor, _last_tick
    with _lock:
        if _monitor is None or not _monitor.is_alive():
            _stop.clear()
            _monitor = threading.Thread(target=_run, name="memory-observer", daemon=True)
            try:
                _monitor.start()
            except RuntimeError:
                _monitor = None
                return
        now = time.monotonic()
        emit = now - _last_tick >= 60
        if emit:
            _last_tick = now
    if emit:
        _emit("periodic")


@contextmanager
def memory_operation(operation, *, correlation=None):
    if not _enabled() or operation not in _OPERATIONS:
        yield
        return
    memory_tick()
    identity = uuid.uuid4().hex[:12]
    correlation = correlation if isinstance(correlation, str) and re.fullmatch(r"[0-9a-f]{12}|[0-9a-f]{32}", correlation) else None
    facts = {"operation": operation, "operationId": identity}
    if correlation:
        facts["resolutionId"] = correlation
    with _lock:
        admitted = len(_active) < 16
        if admitted:
            _active[identity] = {**facts}
    if not admitted:
        yield
        return
    _emit("begin", **facts)
    try:
        yield
    finally:
        with _lock:
            _active.pop(identity, None)
        _emit("end", **facts)


def stop_memory_monitor():
    _stop.set()
