from __future__ import annotations

import hashlib
import json
import pathlib
import threading
import time
from typing import Any, Dict, Iterable, List

from ..domain.catalog import (
    normalize_artist_name,
    normalize_track_title,
    verified_playback_source,
)
from ..recommend.store_runtime import open_recommendation_store_connection
from .intelligence import (
    backfill_canonical_catalog,
    enrich_query_with_musicbrainz,
    load_catalog_entity_memories,
    remember_catalog_entity,
    remember_source_identity,
)
from .server_adapter import SearchServerAdapter

_INFLIGHT: set[str] = set()
_LAST_RUN: dict[str, float] = {}
_INFLIGHT_EVENTS: dict[str, threading.Event] = {}
_LOCK = threading.Lock()
DEFAULT_ACCEPTANCE_FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "catalog_acceptance_fixtures.json"
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _read_field(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _json_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _row_count(row: Any) -> int:
    if row is None:
        return 0
    try:
        return int(row["count"] or 0)
    except Exception:
        return 0


def load_catalog_acceptance_fixtures(path: str | pathlib.Path | None = None) -> List[Dict[str, Any]]:
    fixture_path = pathlib.Path(path) if path else DEFAULT_ACCEPTANCE_FIXTURE_PATH
    try:
        raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if isinstance(raw, dict):
        raw = raw.get("fixtures") or []
    if not isinstance(raw, list):
        return []
    fixtures: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        query = _text(item.get("query"))
        expected_title = _text(item.get("expected_title") or item.get("title"))
        expected_artist = _text(item.get("expected_artist") or item.get("artist"))
        key = normalize_track_title(query)
        if not query or not expected_title or not key or key in seen:
            continue
        seen.add(key)
        fixtures.append(
            {
                "query": query,
                "expected_title": expected_title,
                "expected_artist": expected_artist,
                "category": _text(item.get("category") or "track"),
                "priority": float(item.get("priority") or 1.0),
            }
        )
    return fixtures


def _track_id(track: Dict[str, Any]) -> str:
    return _text(track.get("id") or track.get("videoId") or track.get("video_id"))


def _track_title(track: Dict[str, Any]) -> str:
    return _text(track.get("title") or track.get("name"))


def _track_artist(track: Dict[str, Any]) -> str:
    artist = track.get("artist")
    if isinstance(artist, dict):
        artist = artist.get("name")
    return _text(
        artist
        or track.get("artist_name")
        or track.get("channel")
        or track.get("author")
        or track.get("uploader")
    )


def _track_album(track: Dict[str, Any]) -> str:
    album = track.get("album")
    if isinstance(album, dict):
        album = album.get("title") or album.get("name")
    return _text(album or track.get("album_name"))


def _catalog_seed_key(provider: str, query: str, seed_type: str) -> str:
    payload = {
        "provider": _text(provider).lower() or "musicbrainz",
        "query": normalize_track_title(query),
        "seed_type": _text(seed_type).lower() or "query",
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _seed_dict(value: Any, *, seed_type: str = "query", priority: float = 0.0) -> Dict[str, Any]:
    if isinstance(value, dict):
        query = _text(value.get("query") or value.get("text") or value.get("value"))
        return {
            "query": query,
            "seed_type": _text(value.get("seed_type") or value.get("type") or seed_type) or seed_type,
            "priority": float(value.get("priority") or priority or 0.0),
            "expected_title": _text(value.get("expected_title") or value.get("title")),
            "expected_artist": _text(value.get("expected_artist") or value.get("artist")),
        }
    return {"query": _text(value), "seed_type": seed_type, "priority": float(priority or 0.0)}


def _seed_sources(*, req: Any | None, profile: Any | None, taste: Any | None) -> Iterable[Any]:
    for source in (taste, profile, req):
        if source is None:
            continue
        for key in (
            "last_played_tracks",
            "recent_track_snapshots",
            "top_track_snapshots",
            "anchor_track_snapshots",
            "recent_tracks",
            "seed_tracks",
            "tracks",
        ):
            yield from _as_list(_read_field(source, key))


def collect_catalog_seed_tracks(
    *,
    req: Any | None = None,
    profile: Any | None = None,
    taste: Any | None = None,
    limit: int = 72,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _seed_sources(req=req, profile=profile, taste=taste):
        track = _mapping(raw)
        if not track:
            continue
        title = _track_title(track)
        artist = _track_artist(track)
        if not title:
            continue
        identity = _track_id(track) or f"{normalize_track_title(title)}|{normalize_artist_name(artist)}"
        if not identity or identity in seen:
            continue
        seen.add(identity)
        results.append(track)
        if len(results) >= max(int(limit or 0), 1):
            break
    return results


def _query_variants_for_track(track: Dict[str, Any]) -> List[str]:
    title = _track_title(track)
    artist = _track_artist(track)
    album = _track_album(track)
    variants = [title]
    if title and artist:
        variants.extend(
            [
                f"{title} {artist}",
                f"{artist} {title}",
            ]
        )
    if title and album:
        variants.append(f"{title} {album}")
    compact_artist = normalize_artist_name(artist)
    if title and compact_artist:
        aliases = {
            "guns n roses": "gnr",
            "ac dc": "acdc",
        }
        alias = aliases.get(compact_artist)
        if alias:
            variants.extend([f"{alias} {title}", f"{title} {alias}"])
    deduped: List[str] = []
    seen: set[str] = set()
    for variant in variants:
        normalized = normalize_track_title(variant)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(_text(variant))
    return deduped


def _query_variants_for_catalog_entity(
    *,
    title: str,
    artist: str,
    album: str = "",
    aliases: Iterable[Any] | None = None,
) -> List[str]:
    values: List[str] = []

    def add(value: Any) -> None:
        text = _text(value)
        if text:
            values.append(text)

    add(title)
    if title and artist:
        add(f"{title} {artist}")
        add(f"{artist} {title}")
    if title and album:
        add(f"{title} {album}")
    if album and artist:
        add(f"{album} {artist}")
    artist_key = normalize_artist_name(artist)
    title_key = normalize_track_title(title)
    if title_key and artist_key:
        compact_artist = " ".join(
            token
            for token in artist_key.split()
            if token not in {"the", "and", "n", "of", "a", "an"}
        )
        initials = "".join(
            token[0]
            for token in artist_key.split()
            if token and token not in {"the", "and", "of", "a", "an"}
        )
        if compact_artist and compact_artist != artist_key:
            add(f"{title} {compact_artist}")
            add(f"{compact_artist} {title}")
        if len(initials) >= 2:
            add(f"{initials} {title}")
            add(f"{title} {initials}")
    for alias in aliases or []:
        add(alias)

    deduped: List[str] = []
    seen: set[str] = set()
    for value in values:
        key = normalize_track_title(value)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(_text(value))
    return deduped


def external_catalog_import_progress(server: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "queue_by_status": {},
        "queue_total": 0,
        "catalog_entities": {},
        "catalog_total": 0,
        "alias_total": 0,
        "source_total": 0,
        "playable_source_total": 0,
        "learned_entity_total": 0,
        "error": "",
    }
    try:
        connection = open_recommendation_store_connection(server)
    except Exception as exc:
        result["error"] = str(exc)[:240]
        return result
    try:
        queue_rows = connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM external_catalog_import_queue
            GROUP BY status
            """
        ).fetchall()
        for row in queue_rows:
            status = _text(row["status"]) or "unknown"
            count = int(row["count"] or 0)
            result["queue_by_status"][status] = count
            result["queue_total"] += count
        catalog_rows = connection.execute(
            """
            SELECT entity_type, COUNT(*) AS count
            FROM catalog_entities
            GROUP BY entity_type
            """
        ).fetchall()
        for row in catalog_rows:
            entity_type = _text(row["entity_type"]) or "unknown"
            count = int(row["count"] or 0)
            result["catalog_entities"][entity_type] = count
            result["catalog_total"] += count
        alias_row = connection.execute(
            "SELECT COUNT(*) AS count FROM catalog_entity_aliases"
        ).fetchone()
        source_row = connection.execute(
            "SELECT COUNT(*) AS count FROM catalog_entity_sources"
        ).fetchone()
        playable_row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM catalog_entity_sources
            WHERE source_provider NOT IN ('', 'musicbrainz')
              AND source_key <> ''
            """
        ).fetchone()
        learned_row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM catalog_entities
            WHERE learned_popularity > 0 OR popularity > 0 OR confidence >= 0.75
            """
        ).fetchone()
        result["alias_total"] = _row_count(alias_row)
        result["source_total"] = _row_count(source_row)
        result["playable_source_total"] = _row_count(playable_row)
        result["learned_entity_total"] = _row_count(learned_row)
        track_total = int((result.get("catalog_entities") or {}).get("track") or 0)
        result["track_playable_source_ratio"] = round(
            (float(result["playable_source_total"]) / track_total) if track_total else 0.0,
            4,
        )
    except Exception as exc:
        result["error"] = str(exc)[:240]
    finally:
        connection.close()
    return result


def catalog_import_coverage_report(
    server: Any,
    *,
    fixtures: Iterable[Any] | None = None,
) -> Dict[str, Any]:
    """Return measurable catalog readiness without triggering network import.

    This is the stop-condition surface for catalog work: it tells us whether
    the local canonical memory has enough aliases, sources, and fixture coverage
    to serve production-grade search for this app.
    """
    progress = external_catalog_import_progress(server)
    normalized_fixtures = [_seed_dict(value) for value in (fixtures or [])]
    fixture_results: List[Dict[str, Any]] = []
    passed = 0
    failure_summary: Dict[str, int] = {}
    backfill_queries: List[str] = []
    for fixture in normalized_fixtures:
        query = _text(fixture.get("query"))
        expected_title = normalize_track_title(fixture.get("expected_title") or fixture.get("title") or query)
        expected_artist = normalize_artist_name(fixture.get("expected_artist") or fixture.get("artist") or "")
        memories = load_catalog_entity_memories(
            server,
            query=query,
            entity_type="track",
            limit=5,
        )
        best = memories[0] if memories else {}
        title_key = _text(best.get("title_key"))
        artist_key = _text(best.get("artist_key"))
        title_ok = bool(expected_title and title_key == expected_title)
        artist_ok = bool(
            not expected_artist
            or artist_key == expected_artist
            or (expected_artist in artist_key if artist_key else False)
            or (artist_key in expected_artist if artist_key else False)
        )
        ok = bool(title_ok and artist_ok)
        if ok:
            passed += 1
            failure_reason = ""
        elif not title_key and not artist_key:
            failure_reason = "missing_resolution"
        elif not title_ok:
            failure_reason = "wrong_title"
        elif not artist_ok:
            failure_reason = "wrong_artist"
        else:
            failure_reason = "unknown"
        if failure_reason:
            failure_summary[failure_reason] = failure_summary.get(failure_reason, 0) + 1
            if query and len(backfill_queries) < 40:
                backfill_queries.append(query)
        fixture_results.append(
            {
                "query": query,
                "expected_title": expected_title,
                "expected_artist": expected_artist,
                "resolved_title": title_key,
                "resolved_artist": artist_key,
                "confidence": float(best.get("confidence") or 0.0) if best else 0.0,
                "passed": ok,
                "failure_reason": failure_reason,
            }
        )
    total = len(fixture_results)
    pass_rate = (passed / total) if total else 0.0
    return {
        **progress,
        "fixture_total": total,
        "fixture_passed": passed,
        "fixture_pass_rate": round(pass_rate, 4),
        "fixture_failure_summary": failure_summary,
        "fixture_backfill_queries": backfill_queries,
        "fixture_results": fixture_results,
        "production_usable": bool(
            (not total or pass_rate >= 0.9)
            and int(progress.get("catalog_total") or 0) > 0
            and int(progress.get("alias_total") or 0) > 0
            and float(progress.get("track_playable_source_ratio") or 0.0) >= 0.55
        ),
    }


def enqueue_external_catalog_seeds(
    server: Any,
    seeds: Iterable[Any],
    *,
    user_scope_id: str,
    provider: str = "musicbrainz",
    source: str = "catalog_backfill",
) -> Dict[str, Any]:
    scope = _text(user_scope_id) or "guest"
    normalized_provider = _text(provider).lower() or "musicbrainz"
    now = time.time()
    rows: List[tuple[Any, ...]] = []
    seen: set[str] = set()
    for raw_seed in seeds:
        seed = _seed_dict(raw_seed, seed_type=source, priority=0.5)
        query = _text(seed.get("query"))
        query_key = normalize_track_title(query)
        if not query_key or query_key in seen:
            continue
        seen.add(query_key)
        seed_type = _text(seed.get("seed_type")) or source
        seed_key = _catalog_seed_key(normalized_provider, query, seed_type)
        rows.append(
            (
                seed_key,
                normalized_provider,
                query,
                seed_type,
                scope,
                float(seed.get("priority") or 0.0),
                "pending",
                now,
                now,
            )
        )
    result = {"queued": 0, "deduped": len(seen), "error": ""}
    if not rows:
        return result
    try:
        connection = open_recommendation_store_connection(server)
    except Exception as exc:
        result["error"] = str(exc)[:240]
        return result
    try:
        connection.executemany(
            """
            INSERT INTO external_catalog_import_queue(
                seed_key, provider, query, seed_type, user_scope_id,
                priority, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(seed_key) DO UPDATE SET
                user_scope_id = excluded.user_scope_id,
                priority = max(external_catalog_import_queue.priority, excluded.priority),
                status = CASE
                    WHEN external_catalog_import_queue.status IN ('completed', 'no_results')
                    THEN external_catalog_import_queue.status
                    ELSE 'pending'
                END,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        connection.commit()
        result["queued"] = len(rows)
    except Exception as exc:
        result["error"] = str(exc)[:240]
    finally:
        connection.close()
    return result


def _append_catalog_backfill_seed(
    seeds: List[Dict[str, Any]],
    seen: set[str],
    query: str,
    *,
    seed_type: str,
    priority: float,
) -> None:
    normalized_query = _text(query)
    query_key = normalize_track_title(normalized_query)
    if not query_key or query_key in seen:
        return
    seen.add(query_key)
    seeds.append(
        {
            "query": normalized_query,
            "seed_type": seed_type,
            "priority": float(priority or 0.0),
        }
    )


def collect_external_catalog_backfill_seeds(
    server: Any,
    *,
    user_scope_id: str = "guest",
    limit: int = 160,
) -> List[Dict[str, Any]]:
    """Collect import seeds from real app evidence, not a hand-written song list."""
    max_limit = max(int(limit or 0), 0)
    if max_limit <= 0:
        return []
    scope = _text(user_scope_id) or "guest"
    seeds: List[Dict[str, Any]] = []
    seen: set[str] = set()
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return []
    try:
        search_rows = connection.execute(
            """
            SELECT query,
                   COUNT(*) AS event_count,
                   MAX(occurred_at) AS latest_at
            FROM recommendation_search_events
            WHERE TRIM(query) <> ''
              AND (? IN ('catalog', 'global', 'guest') OR user_scope_id IN (?, 'catalog', 'global'))
            GROUP BY query
            ORDER BY event_count DESC, latest_at DESC
            LIMIT ?
            """,
            [scope.lower(), scope, max_limit],
        ).fetchall()
        for row in search_rows:
            count = int(row["event_count"] or 0)
            _append_catalog_backfill_seed(
                seeds,
                seen,
                row["query"],
                seed_type="stored_search_query",
                priority=min(1.58, 1.42 + (count * 0.035)),
            )
            if len(seeds) >= max_limit:
                return seeds

        alias_rows = connection.execute(
            """
            SELECT alias_key, title_key, artist_key, score, confidence, event_count
            FROM search_query_aliases
            WHERE alias_key <> ''
            ORDER BY score DESC, confidence DESC, event_count DESC, updated_at DESC
            LIMIT ?
            """,
            [max_limit * 2],
        ).fetchall()
        for row in alias_rows:
            title = _text(row["title_key"]).replace("  ", " ")
            artist = _text(row["artist_key"]).replace("  ", " ")
            query = " ".join(part for part in [title, artist] if part).strip()
            if not query:
                query = _text(row["alias_key"])
            confidence = float(row["confidence"] or 0.0)
            score = float(row["score"] or 0.0)
            _append_catalog_backfill_seed(
                seeds,
                seen,
                query,
                seed_type="stored_query_alias",
                priority=min(1.50, 1.12 + (confidence * 0.24) + (score * 0.08)),
            )
            if len(seeds) >= max_limit:
                return seeds

        entity_rows = connection.execute(
            """
            SELECT display_title, display_artist, display_album,
                   confidence, popularity, learned_popularity
            FROM catalog_entities
            WHERE entity_type = 'track'
              AND display_title <> ''
            ORDER BY learned_popularity DESC, popularity DESC, confidence DESC, updated_at DESC
            LIMIT ?
            """,
            [max_limit * 2],
        ).fetchall()
        for row in entity_rows:
            title = _text(row["display_title"])
            artist = _text(row["display_artist"])
            album = _text(row["display_album"])
            query = " ".join(part for part in [title, artist] if part).strip()
            if not query and album and artist:
                query = f"{album} {artist}"
            confidence = float(row["confidence"] or 0.0)
            popularity = max(float(row["popularity"] or 0.0), float(row["learned_popularity"] or 0.0))
            _append_catalog_backfill_seed(
                seeds,
                seen,
                query,
                seed_type="stored_catalog_entity",
                priority=min(1.08, 0.78 + (confidence * 0.18) + (popularity * 0.08)),
            )
            if len(seeds) >= max_limit:
                return seeds
    except Exception:
        return seeds
    finally:
        connection.close()
    return seeds


def _next_external_catalog_imports(
    server: Any,
    *,
    provider: str,
    batch_size: int,
    max_attempts: int,
) -> List[Dict[str, Any]]:
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return []
    try:
        rows = connection.execute(
            """
            SELECT seed_key, provider, query, seed_type, user_scope_id,
                   priority, attempt_count
            FROM external_catalog_import_queue
            WHERE provider = ?
              AND status IN ('pending', 'retry')
              AND attempt_count < ?
            ORDER BY priority DESC, attempt_count ASC, updated_at ASC
            LIMIT ?
            """,
            [
                _text(provider).lower() or "musicbrainz",
                max(int(max_attempts or 0), 1),
                max(int(batch_size or 0), 1),
            ],
        ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []
    finally:
        connection.close()


def _mark_external_catalog_import(
    server: Any,
    *,
    seed_key: str,
    status: str,
    imported_count: int = 0,
    error: str = "",
) -> None:
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return
    now = time.time()
    try:
        connection.execute(
            """
            UPDATE external_catalog_import_queue
            SET status = ?,
                attempt_count = attempt_count + 1,
                imported_count = ?,
                last_error = ?,
                updated_at = ?,
                processed_at = CASE
                    WHEN ? IN ('completed', 'no_results', 'failed')
                    THEN ?
                    ELSE processed_at
                END
            WHERE seed_key = ?
            """,
            [
                _text(status) or "pending",
                max(int(imported_count or 0), 0),
                _text(error)[:240],
                now,
                _text(status) or "pending",
                now,
                _text(seed_key),
            ],
        )
        connection.commit()
    except Exception:
        pass
    finally:
        connection.close()


def run_external_catalog_import(
    server: Any,
    *,
    user_scope_id: str = "guest",
    provider: str = "musicbrainz",
    batch_size: int = 6,
    max_attempts: int = 3,
    musicbrainz_client: Any | None = None,
) -> Dict[str, Any]:
    normalized_provider = _text(provider).lower() or "musicbrainz"
    result = {
        "provider": normalized_provider,
        "processed": 0,
        "imported": 0,
        "completed": 0,
        "no_results": 0,
        "failed": 0,
        "retry": 0,
        "error": "",
    }
    if normalized_provider != "musicbrainz":
        result["error"] = "unsupported_provider"
        return result
    rows = _next_external_catalog_imports(
        server,
        provider=normalized_provider,
        batch_size=batch_size,
        max_attempts=max_attempts,
    )
    for row in rows:
        query = _text(row.get("query"))
        seed_key = _text(row.get("seed_key"))
        scope = _text(row.get("user_scope_id")) or _text(user_scope_id) or "guest"
        if not query or not seed_key:
            continue
        result["processed"] += 1
        enrichment = enrich_query_with_musicbrainz(
            server,
            user_scope_id=scope,
            query=query,
            limit=6,
            client=musicbrainz_client,
        )
        imported = int(enrichment.get("imported") or 0)
        result["imported"] += imported
        if enrichment.get("error"):
            attempts = int(row.get("attempt_count") or 0) + 1
            status = "failed" if attempts >= max(int(max_attempts or 0), 1) else "retry"
            result[status] += 1
            _mark_external_catalog_import(
                server,
                seed_key=seed_key,
                status=status,
                imported_count=imported,
                error=_text(enrichment.get("error")),
            )
            continue
        if imported > 0 or int(enrichment.get("candidate_count") or 0) > 0:
            result["completed"] += 1
            _mark_external_catalog_import(
                server,
                seed_key=seed_key,
                status="completed",
                imported_count=imported,
            )
        else:
            result["no_results"] += 1
            _mark_external_catalog_import(
                server,
                seed_key=seed_key,
                status="no_results",
                imported_count=0,
            )
    return result


def populate_catalog_from_user_signals(
    server: Any,
    *,
    user_scope_id: str,
    req: Any | None = None,
    profile: Any | None = None,
    taste: Any | None = None,
    track_limit: int = 72,
    run_backfill: bool = True,
    source: str = "user_history_catalog_seed",
) -> Dict[str, Any]:
    """Seed canonical catalog memory from trusted user-visible history.

    This does not claim that every listened item is globally canonical. It gives
    the resolver stable playable anchors, aliases, and source identities so
    repeated searches do not depend only on provider order.
    """
    scope = _text(user_scope_id) or "guest"
    result: Dict[str, Any] = {
        "seed_tracks": 0,
        "stored_track_aliases": 0,
        "stored_source_identities": 0,
        "backfill_processed": 0,
        "error": "",
    }
    try:
        tracks = collect_catalog_seed_tracks(
            req=req,
            profile=profile,
            taste=taste,
            limit=track_limit,
        )
        result["seed_tracks"] = len(tracks)
        for index, track in enumerate(tracks):
            variants = _query_variants_for_track(track)
            if not variants:
                continue
            confidence = max(0.62, 0.9 - (index * 0.003))
            weight = max(0.25, 1.0 - (index * 0.01))
            for variant in variants[:4]:
                if remember_catalog_entity(
                    server,
                    user_scope_id=scope,
                    query=variant,
                    entity_type="track",
                    item=track,
                    confidence=confidence,
                    event_weight=weight,
                    event_type="user_history_seed",
                    source=source,
                ):
                    result["stored_track_aliases"] += 1
            if remember_source_identity(server, track, confidence_floor=0.54):
                result["stored_source_identities"] += 1

        if run_backfill:
            backfill = backfill_canonical_catalog(
                server,
                search_event_limit=80,
                canonical_entity_limit=80,
                musicbrainz_query_limit=0,
            )
            result["backfill_processed"] = int(backfill.get("stored_entities") or 0)
    except Exception as exc:
        result["error"] = str(exc)[:240]
    return result


def catalog_playable_tracks_for_query(
    server: Any,
    *,
    user_scope_id: str,
    query: str,
    limit: int = 4,
) -> List[Dict[str, Any]]:
    scope = _text(user_scope_id) or "guest"
    if scope.lower() in {"guest", "public", "anonymous"}:
        return []
    search = SearchServerAdapter(server)
    memories = load_catalog_entity_memories(
        server,
        query=query,
        entity_type="track",
        limit=max(int(limit or 0), 1) * 3,
    )
    output: List[Dict[str, Any]] = []
    seen: set[str] = set()
    query_key = normalize_track_title(query)
    for memory in memories:
        payload = _mapping(memory.get("payload"))
        if not payload:
            continue
        track_id = _track_id(payload)
        if not track_id:
            continue
        if not _catalog_payload_is_playable(payload):
            continue
        identity = track_id or f"{normalize_track_title(_track_title(payload))}|{normalize_artist_name(_track_artist(payload))}"
        if not identity or identity in seen:
            continue
        title_key = normalize_track_title(_track_title(payload))
        artist_key = normalize_artist_name(_track_artist(payload))
        if query_key and title_key and query_key not in {title_key, f"{title_key} {artist_key}".strip(), f"{artist_key} {title_key}".strip()}:
            tokens = set(search.query_tokens(query))
            title_tokens = set(search.query_tokens(_track_title(payload)))
            artist_tokens = set(search.query_tokens(_track_artist(payload)))
            if tokens and len(tokens & (title_tokens | artist_tokens)) < max(1, min(2, len(tokens))):
                continue
        seen.add(identity)
        item = dict(payload)
        item["catalog_memory_match"] = True
        item["catalog_entity_confidence"] = max(
            float(memory.get("confidence") or 0.0),
            float(item.get("catalog_entity_confidence") or 0.0),
        )
        item["learned_popularity"] = max(
            float(memory.get("learned_popularity") or 0.0),
            float(item.get("learned_popularity") or 0.0),
        )
        if not item.get("source_authority"):
            item["source_authority"] = memory.get("source_authority") or "catalog_memory"
        output.append(item)
    output.sort(
        key=lambda item: (
            float(item.get("catalog_entity_confidence") or 0.0),
            float(item.get("learned_popularity") or 0.0),
            float(item.get("source_quality_score") or 0.0),
        ),
        reverse=True,
    )
    return output[: max(int(limit or 0), 1)]


def _catalog_payload_is_playable(payload: Dict[str, Any]) -> bool:
    return bool(verified_playback_source(payload))


def _catalog_memory_track_item(row: Any) -> Dict[str, Any]:
    payload = _json_mapping(row["payload_json"])
    item = dict(payload)
    if not item.get("title") and _text(row["display_title"]):
        item["title"] = _text(row["display_title"])
    if not item.get("artist") and _text(row["display_artist"]):
        item["artist"] = _text(row["display_artist"])
    if not item.get("album") and _text(row["display_album"]):
        item["album"] = _text(row["display_album"])
    item["catalog_memory_match"] = True
    item["catalog_entity_confidence"] = max(
        float(row["confidence"] or 0.0),
        float(item.get("catalog_entity_confidence") or 0.0),
    )
    item["learned_popularity"] = max(
        float(row["learned_popularity"] or 0.0),
        float(item.get("learned_popularity") or 0.0),
    )
    item.setdefault("source_authority", "catalog_memory")
    return item


def _catalog_memory_album_item(row: Any) -> Dict[str, Any]:
    payload = _json_mapping(row["payload_json"])
    item = dict(payload)
    if not item.get("title") and _text(row["display_title"]):
        item["title"] = _text(row["display_title"])
    if not item.get("artist") and _text(row["display_artist"]):
        item["artist"] = _text(row["display_artist"])
    item["catalog_memory_match"] = True
    item["catalog_entity_confidence"] = max(
        float(row["confidence"] or 0.0),
        float(item.get("catalog_entity_confidence") or 0.0),
    )
    item["learned_popularity"] = max(
        float(row["learned_popularity"] or 0.0),
        float(item.get("learned_popularity") or 0.0),
    )
    item.setdefault("source_authority", "catalog_memory")
    return item


def catalog_album_is_detail_ready(payload: Dict[str, Any]) -> bool:
    """Match search publication to the album-detail contract."""
    if not isinstance(payload, dict):
        return False
    album_id = _text(
        payload.get("id")
        or payload.get("album_id")
        or payload.get("albumId")
        or payload.get("browseId")
    )
    release_group_id = _text(
        payload.get("musicbrainz_release_group_id")
        or payload.get("mb_release_group_id")
    )
    # A real YTMusic browse id remains detail-ready after MusicBrainz metadata
    # is attached. Canonical enrichment must not turn a usable provider album
    # back into an unresolved MusicBrainz-only placeholder.
    if album_id.startswith("MPRE"):
        return True
    is_canonical_release = bool(release_group_id) or album_id.startswith(
        "musicbrainz:release-group:"
    )
    if is_canonical_release:
        tracks = [
            item
            for item in (
                payload.get("tracks")
                or payload.get("canonical_tracks")
                or []
            )
            if isinstance(item, dict)
            and _text(item.get("track_key"))
            and item.get("playable") is not False
        ]
        return payload.get("playable") is True and bool(tracks)
    # The non-canonical detail route calls YTMusic get_album. Only a real
    # YTMusic album browse ID can satisfy that route; recording titles, video
    # IDs and text-search placeholders must not be published as albums.
    return False


def _artist_key_matches(candidate_artist: str, requested_artist: str) -> bool:
    candidate_key = normalize_artist_name(candidate_artist)
    requested_key = normalize_artist_name(requested_artist)
    if not candidate_key or not requested_key:
        return False
    if candidate_key == requested_key:
        return True
    candidate_tokens = {token for token in candidate_key.split() if len(token) > 1}
    requested_tokens = {token for token in requested_key.split() if len(token) > 1}
    if not candidate_tokens or not requested_tokens:
        return False
    overlap = len(candidate_tokens & requested_tokens)
    return overlap >= max(2, min(len(candidate_tokens), len(requested_tokens)))


def catalog_playable_tracks_for_artist(
    server: Any,
    *,
    user_scope_id: str,
    artist: str,
    limit: int = 24,
) -> List[Dict[str, Any]]:
    """Return known playable catalog tracks for one artist.

    This is the feed/radio-friendly lookup missing from the older exact-query
    catalog path. It lets MusicBrainz/catalog identity drive candidate choice,
    while YTMusic remains only the background playable-source bridge.
    """

    artist_name = _text(artist)
    artist_key = normalize_artist_name(artist_name)
    if not artist_key:
        return []
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return []
    try:
        rows = connection.execute(
            """
            SELECT entity_key, display_title, display_artist, display_album,
                   confidence, popularity, learned_popularity, payload_json, updated_at
            FROM catalog_entities
            WHERE entity_type = 'track'
              AND (
                    lower(trim(display_artist)) = lower(trim(?))
                    OR lower(display_artist) LIKE lower(?)
                  )
            ORDER BY CASE
                       WHEN lower(trim(display_artist)) = lower(trim(?)) THEN 1
                       ELSE 0
                     END DESC,
                     learned_popularity DESC, popularity DESC,
                     confidence DESC, updated_at DESC
            LIMIT ?
            """,
            [
                artist_name,
                f"%{artist_name}%",
                artist_name,
                max(int(limit or 0), 1) * 24,
            ],
        ).fetchall()
    except Exception:
        return []
    finally:
        connection.close()

    output: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = _catalog_memory_track_item(row)
        if not _catalog_payload_is_playable(item):
            continue
        candidate_artist = _track_artist(item) or _text(row["display_artist"])
        if not _artist_key_matches(candidate_artist, artist_name):
            continue
        identity = _track_id(item) or f"{normalize_track_title(_track_title(item))}|{normalize_artist_name(candidate_artist)}"
        if not identity or identity in seen:
            continue
        seen.add(identity)
        item["catalog_artist_match"] = True
        item["catalog_artist_query"] = artist_name
        output.append(item)
        if len(output) >= max(int(limit or 0), 1):
            break
    return output


def catalog_albums_for_artist(
    server: Any,
    *,
    artist: str,
    limit: int = 24,
) -> List[Dict[str, Any]]:
    """Return persisted canonical albums for one artist without live retrieval."""

    artist_name = _text(artist)
    if not normalize_artist_name(artist_name):
        return []
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return []
    try:
        rows = connection.execute(
            """
            SELECT entity_key, display_title, display_artist, display_album,
                   confidence, popularity, learned_popularity, payload_json, updated_at
            FROM catalog_entities
            WHERE entity_type = 'album'
              AND lower(display_artist) LIKE lower(?)
            ORDER BY learned_popularity DESC, popularity DESC,
                     confidence DESC, updated_at DESC
            LIMIT ?
            """,
            [
                f"%{artist_name}%",
                max(int(limit or 0), 1) * 24,
            ],
        ).fetchall()
    except Exception:
        return []
    finally:
        connection.close()

    output: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = _catalog_memory_album_item(row)
        if not catalog_album_is_detail_ready(item):
            continue
        candidate_artist = _text(
            item.get("artist")
            or item.get("artist_name")
            or row["display_artist"]
        )
        if not _artist_key_matches(candidate_artist, artist_name):
            continue
        identity = _text(
            item.get("canonical_album_identity")
            or item.get("musicbrainz_release_group_id")
            or item.get("id")
            or row["entity_key"]
        )
        if not identity or identity in seen:
            continue
        seen.add(identity)
        item["catalog_artist_match"] = True
        item["catalog_artist_query"] = artist_name
        output.append(item)
        if len(output) >= max(int(limit or 0), 1):
            break
    return output


def catalog_playable_backbone_tracks(
    server: Any,
    *,
    limit: int = 160,
) -> List[Dict[str, Any]]:
    """Return the shared canonical playable backbone without live retrieval."""

    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return []
    try:
        rows = connection.execute(
            """
            SELECT entity_key, display_title, display_artist, display_album,
                   confidence, popularity, learned_popularity, payload_json, updated_at
            FROM catalog_entities
            WHERE entity_type = 'track'
            ORDER BY learned_popularity DESC, popularity DESC, confidence DESC, updated_at DESC
            LIMIT ?
            """,
            [max(int(limit or 0), 1) * 4],
        ).fetchall()
    except Exception:
        return []
    finally:
        connection.close()

    output: List[Dict[str, Any]] = []
    seen: set[str] = set()
    artist_counts: Dict[str, int] = {}
    for row in rows:
        item = _catalog_memory_track_item(row)
        if not _catalog_payload_is_playable(item):
            continue
        identity = _track_id(item) or _text(row["entity_key"])
        artist = _track_artist(item) or _text(row["display_artist"])
        artist_key = normalize_artist_name(artist)
        if not identity or identity in seen:
            continue
        if artist_key and artist_counts.get(artist_key, 0) >= 3:
            continue
        seen.add(identity)
        if artist_key:
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        item["relationship_provider"] = "shared_catalog"
        item["relationship_evidence"] = "canonical_playable_backbone"
        item["recommendation_path"] = "broad_global"
        item["source_provenance"] = "structured:shared_catalog_backbone"
        output.append(item)
        if len(output) >= max(int(limit or 0), 1):
            break
    return output


def schedule_catalog_population(
    server: Any,
    *,
    user_scope_id: str,
    req: Any | None = None,
    profile: Any | None = None,
    taste: Any | None = None,
    reason: str = "background",
    min_interval_seconds: float = 60.0,
    wait_for_completion: bool = False,
    wait_timeout_seconds: float = 80.0,
) -> Dict[str, Any]:
    tracks = collect_catalog_seed_tracks(req=req, profile=profile, taste=taste, limit=24)
    fingerprint_payload = {
        "scope": _text(user_scope_id) or "guest",
        "tracks": [
            _track_id(track) or f"{normalize_track_title(_track_title(track))}|{normalize_artist_name(_track_artist(track))}"
            for track in tracks[:12]
        ],
    }
    fingerprint = hashlib.sha1(
        json.dumps(fingerprint_payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    now = time.time()
    wait_event: threading.Event | None = None
    created_population = False
    with _LOCK:
        if fingerprint in _INFLIGHT:
            wait_event = _INFLIGHT_EVENTS.get(fingerprint)
            if not wait_for_completion:
                return {"scheduled": False, "reason": "inflight", "fingerprint": fingerprint}
        if now - _LAST_RUN.get(fingerprint, 0.0) < max(float(min_interval_seconds or 0.0), 0.0):
            return {"scheduled": False, "reason": "recent", "fingerprint": fingerprint}
        if wait_event is None:
            _INFLIGHT.add(fingerprint)
            wait_event = threading.Event()
            _INFLIGHT_EVENTS[fingerprint] = wait_event
            created_population = True

    if not created_population and wait_for_completion and wait_event is not None:
        completed = wait_event.wait(timeout=max(float(wait_timeout_seconds or 0.0), 0.1))
        return {
            "scheduled": False,
            "reason": "inflight_completed" if completed else "inflight_timeout",
            "fingerprint": fingerprint,
            "completed": completed,
        }

    def worker() -> None:
        try:
            populate_catalog_from_user_signals(
                server,
                user_scope_id=user_scope_id,
                req=req,
                profile=profile,
                taste=taste,
                source=f"{reason}_catalog_seed",
            )
        finally:
            with _LOCK:
                _INFLIGHT.discard(fingerprint)
                _LAST_RUN[fingerprint] = time.time()
                completed_event = _INFLIGHT_EVENTS.pop(fingerprint, None)
                if completed_event is not None:
                    completed_event.set()

    if wait_for_completion:
        worker()
        return {
            "scheduled": False,
            "reason": "completed_inline",
            "fingerprint": fingerprint,
            "completed": True,
        }

    executor = (
        getattr(server, "search_executor", None)
        or getattr(server, "recommendation_executor", None)
    )
    if executor is None:
        worker()
        return {"scheduled": False, "reason": "ran_inline", "fingerprint": fingerprint}
    try:
        executor.submit(worker)
        return {"scheduled": True, "reason": reason, "fingerprint": fingerprint}
    except Exception as exc:
        with _LOCK:
            _INFLIGHT.discard(fingerprint)
        return {"scheduled": False, "reason": "submit_failed", "error": str(exc)[:160]}
