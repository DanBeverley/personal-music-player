"""Opt-in live diagnostic for the MusicBrainz acquisition path.

Run explicitly; normal test suites skip this external-provider check:

    $env:AURALIS_RUN_MUSICBRAINZ_CONNECTIVITY_TEST = "1"
    python -B -m pytest ingestion/python_proxy/test_musicbrainz_connectivity.py -q -s
"""

from __future__ import annotations

import os
import socket
import ssl
import time
from types import SimpleNamespace
from typing import Any

import pytest

from auralis_backend.discovery.enrichment import (
    EnrichmentRequest,
    _resolve_musicbrainz_album_rows,
)
from auralis_backend.search.musicbrainz import (
    MUSICBRAINZ_ACQUISITION_TIMEOUT_SECONDS,
    MUSICBRAINZ_BASE_URL,
    MusicBrainzRequestFailure,
)


_RUN_LIVE = os.environ.get("AURALIS_RUN_MUSICBRAINZ_CONNECTIVITY_TEST", "").strip() == "1"


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _resolved_addresses(host: str) -> list[tuple[int, tuple[Any, ...]]]:
    addresses: list[tuple[int, tuple[Any, ...]]] = []
    seen: set[tuple[int, tuple[Any, ...]]] = set()
    for family, _socktype, _proto, _canonname, address in socket.getaddrinfo(
        host,
        443,
        type=socket.SOCK_STREAM,
    ):
        key = (family, address)
        if key not in seen:
            seen.add(key)
            addresses.append(key)
    return addresses


def _probe_tls(host: str, family: int, address: tuple[Any, ...]) -> str:
    started = time.perf_counter()
    raw = socket.socket(family, socket.SOCK_STREAM)
    raw.settimeout(MUSICBRAINZ_ACQUISITION_TIMEOUT_SECONDS)
    try:
        raw.connect(address)
        context = ssl.create_default_context()
        with context.wrap_socket(raw, server_hostname=host) as connection:
            protocol = connection.version() or "unknown"
            peer = connection.getpeername()
            return f"ok peer={peer!r} tls={protocol} elapsed_ms={_elapsed_ms(started)}"
    except Exception as exc:
        raw.close()
        return (
            f"failed error={type(exc).__name__} detail={exc!s} "
            f"elapsed_ms={_elapsed_ms(started)}"
        )


def _provider_failure_detail(exc: MusicBrainzRequestFailure) -> str:
    cause = exc.__cause__
    headers = getattr(cause, "headers", None)
    header_values: dict[str, str] = {}
    if headers is not None:
        for name in (
            "Retry-After",
            "X-RateLimit-Zone",
            "X-RateLimit-Who",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        ):
            value = str(headers.get(name) or "").strip()
            if value:
                header_values[name] = value
    body = ""
    reader = getattr(cause, "read", None)
    if callable(reader):
        try:
            body = reader(512).decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
    return f"headers={header_values!r} body={body!r}"


@pytest.mark.skipif(
    not _RUN_LIVE,
    reason="set AURALIS_RUN_MUSICBRAINZ_CONNECTIVITY_TEST=1 for the live diagnostic",
)
def test_full_musicbrainz_album_acquisition_request() -> None:
    """Exercise DNS/TCP/TLS and the production artist -> album -> tracks path."""

    host = MUSICBRAINZ_BASE_URL.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    addresses = _resolved_addresses(host)
    print(f"[MB-DIAG] phase=dns host={host} addresses={addresses!r}")
    assert addresses, f"MusicBrainz DNS returned no addresses for {host}"

    for family, address in addresses:
        family_name = "ipv6" if family == socket.AF_INET6 else "ipv4"
        print(
            f"[MB-DIAG] phase=tls family={family_name} address={address!r} "
            f"result={_probe_tls(host, family, address)}"
        )

    request = EnrichmentRequest(
        kind="album_catalog",
        provider="musicbrainz",
        key="Sepultura",
        pool="album",
        relation="artist_catalog_album",
        limit=1,
        metadata={
            "profile_seed_artist": "Sepultura",
            "release_group_offset": 0,
        },
    )
    started = time.perf_counter()
    try:
        rows = _resolve_musicbrainz_album_rows(SimpleNamespace(), request)
    except MusicBrainzRequestFailure as exc:
        cause = exc.__cause__
        pytest.fail(
            "MusicBrainz production acquisition failed: "
            f"phase={exc.path} status={exc.status_code} cause={exc.cause_type} "
            f"detail={cause!s} elapsed_ms={exc.elapsed_ms} "
            f"{_provider_failure_detail(exc)}",
            pytrace=False,
        )

    print(
        f"[MB-DIAG] phase=full_acquisition artist=Sepultura albums={len(rows)} "
        f"tracks={sum(len(row.get('canonical_tracks') or []) for row in rows)} "
        f"elapsed_ms={_elapsed_ms(started)}"
    )
    assert rows, "MusicBrainz responded, but the production acquisition produced no album"
    assert rows[0].get("canonical_tracks"), (
        "MusicBrainz responded, but the selected album had no canonical tracklist"
    )
