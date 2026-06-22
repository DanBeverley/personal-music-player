from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Dict, Iterable, List

from ..domain.catalog import normalize_artist_name, normalize_track_title
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
_LOCK = threading.Lock()


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


def _query_seeds(*, req: Any | None, profile: Any | None, taste: Any | None, limit: int) -> List[str]:
    values: List[str] = []
    for source in (taste, profile, req):
        if source is None:
            continue
        for key in ("recent_queries", "taste_queries", "artist_hints", "album_hints"):
            for value in _as_list(_read_field(source, key)):
                text = _text(value)
                if text:
                    values.append(text)
    seen: set[str] = set()
    deduped: List[str] = []
    for value in values:
        normalized = normalize_track_title(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(value)
        if len(deduped) >= max(int(limit or 0), 0):
            break
    return deduped


def _broader_musicbrainz_query_seeds(
    *,
    req: Any | None = None,
    profile: Any | None = None,
    taste: Any | None = None,
    limit: int = 12,
) -> List[str]:
    values = _query_seeds(req=req, profile=profile, taste=taste, limit=limit)
    for track in collect_catalog_seed_tracks(req=req, profile=profile, taste=taste, limit=48):
        title = _track_title(track)
        artist = _track_artist(track)
        album = _track_album(track)
        if title and artist:
            values.append(f"{title} {artist}")
        elif title:
            values.append(title)
        if album and artist:
            values.append(f"{album} {artist}")
        if artist:
            values.append(artist)
    seen: set[str] = set()
    deduped: List[str] = []
    for value in values:
        normalized = normalize_track_title(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(_text(value))
        if len(deduped) >= max(int(limit or 0), 0):
            break
    return deduped


def external_catalog_import_progress(server: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "queue_by_status": {},
        "queue_total": 0,
        "catalog_entities": {},
        "catalog_total": 0,
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
    except Exception as exc:
        result["error"] = str(exc)[:240]
    finally:
        connection.close()
    return result


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
                priority=min(1.22, 1.08 + (count * 0.025)),
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
                priority=min(1.14, 0.84 + (confidence * 0.18) + (score * 0.04)),
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
    query_limit: int = 8,
    run_musicbrainz: bool = False,
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
        "musicbrainz_queries": 0,
        "musicbrainz_imported": 0,
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

        if run_musicbrainz:
            for query in _query_seeds(req=req, profile=profile, taste=taste, limit=query_limit):
                memories = load_catalog_entity_memories(
                    server,
                    query=query,
                    entity_type="track",
                    limit=1,
                )
                if memories:
                    continue
                enrichment = enrich_query_with_musicbrainz(
                    server,
                    user_scope_id=scope,
                    query=query,
                    limit=3,
                )
                result["musicbrainz_queries"] += 1
                result["musicbrainz_imported"] += int(enrichment.get("imported") or 0)

        if run_backfill:
            backfill = backfill_canonical_catalog(
                server,
                search_event_limit=80,
                canonical_entity_limit=80,
                musicbrainz_query_limit=2 if run_musicbrainz else 0,
            )
            result["backfill_processed"] = int(backfill.get("stored_entities") or 0)
    except Exception as exc:
        result["error"] = str(exc)[:240]
    return result


def broader_catalog_backfill(
    server: Any,
    *,
    user_scope_id: str,
    req: Any | None = None,
    profile: Any | None = None,
    taste: Any | None = None,
    track_limit: int = 128,
    musicbrainz_query_limit: int = 12,
    stored_seed_limit: int = 96,
    musicbrainz_client: Any | None = None,
    source: str = "broader_catalog_backfill",
) -> Dict[str, Any]:
    result = populate_catalog_from_user_signals(
        server,
        user_scope_id=user_scope_id,
        req=req,
        profile=profile,
        taste=taste,
        track_limit=track_limit,
        query_limit=0,
        run_musicbrainz=False,
        source=source,
    )
    result.update(
        {
            "stored_backfill_seeds": 0,
            "broader_musicbrainz_queued": 0,
            "broader_musicbrainz_queries": 0,
            "broader_musicbrainz_imported": 0,
            "broader_backfill_processed": 0,
        }
    )
    try:
        seeds = _broader_musicbrainz_query_seeds(
            req=req,
            profile=profile,
            taste=taste,
            limit=musicbrainz_query_limit,
        )
        prioritized_user_seeds = [
            {"query": query, "seed_type": source, "priority": 1.25}
            for query in seeds
        ]
        stored_seeds = collect_external_catalog_backfill_seeds(
            server,
            user_scope_id=user_scope_id,
            limit=stored_seed_limit,
        )
        result["stored_backfill_seeds"] = len(stored_seeds)
        enqueue_result = enqueue_external_catalog_seeds(
            server,
            [*prioritized_user_seeds, *stored_seeds],
            user_scope_id=user_scope_id,
            provider="musicbrainz",
            source=source,
        )
        result["broader_musicbrainz_queued"] = int(enqueue_result.get("queued") or 0)
        import_result = run_external_catalog_import(
            server,
            user_scope_id=user_scope_id,
            provider="musicbrainz",
            batch_size=max(1, min(int(musicbrainz_query_limit or 0), 12)),
            musicbrainz_client=musicbrainz_client,
        )
        result["broader_musicbrainz_queries"] = int(import_result.get("processed") or 0)
        result["broader_musicbrainz_imported"] = int(import_result.get("imported") or 0)
        backfill = backfill_canonical_catalog(
            server,
            search_event_limit=240,
            canonical_entity_limit=240,
            musicbrainz_query_limit=0,
        )
        result["broader_backfill_processed"] = int(backfill.get("stored_entities") or 0)
    except Exception as exc:
        result["error"] = str(exc)[:240]
    return result


def run_catalog_warmup(
    server: Any,
    *,
    user_scope_id: str = "catalog",
    req: Any | None = None,
    profile: Any | None = None,
    taste: Any | None = None,
    max_queries: int = 24,
    batch_size: int = 4,
    time_budget_seconds: float = 45.0,
    min_interval_seconds: float = 300.0,
    force: bool = False,
    musicbrainz_client: Any | None = None,
    source: str = "catalog_warmup",
) -> Dict[str, Any]:
    """Bounded background catalog warmup from real app evidence.

    This is intentionally small and repeatable: foreground search should never
    wait on catalog import, and a scheduler cycle should not become an
    unbounded MusicBrainz crawler.
    """
    scope = _text(user_scope_id) or "catalog"
    normalized_source = _text(source) or "catalog_warmup"
    fingerprint = f"catalog_warmup:{scope}:{normalized_source}"
    now = time.time()
    min_interval = max(float(min_interval_seconds or 0.0), 0.0)
    with _LOCK:
        if not force and fingerprint in _INFLIGHT:
            return {"status": "skipped", "reason": "inflight", "fingerprint": fingerprint}
        if not force and now - _LAST_RUN.get(fingerprint, 0.0) < min_interval:
            return {"status": "skipped", "reason": "recent", "fingerprint": fingerprint}
        _INFLIGHT.add(fingerprint)

    started = time.perf_counter()
    deadline = started + max(float(time_budget_seconds or 0.0), 1.0)
    query_budget = max(int(max_queries or 0), 0)
    import_batch_size = max(1, min(int(batch_size or 1), 12))
    result: Dict[str, Any] = {
        "status": "completed",
        "fingerprint": fingerprint,
        "scope": scope,
        "source": normalized_source,
        "seed_tracks": 0,
        "stored_backfill_seeds": 0,
        "queued": 0,
        "processed": 0,
        "imported": 0,
        "completed": 0,
        "no_results": 0,
        "failed": 0,
        "retry": 0,
        "backfill_processed": 0,
        "elapsed_ms": 0,
        "budget_ms": int(max(float(time_budget_seconds or 0.0), 1.0) * 1000),
        "error": "",
    }
    try:
        seed_result = populate_catalog_from_user_signals(
            server,
            user_scope_id=scope,
            req=req,
            profile=profile,
            taste=taste,
            track_limit=128,
            query_limit=0,
            run_musicbrainz=False,
            run_backfill=False,
            source=normalized_source,
        )
        result["seed_tracks"] = int(seed_result.get("seed_tracks") or 0)
        if seed_result.get("error"):
            result["error"] = _text(seed_result.get("error"))[:240]

        user_seeds = [
            {"query": query, "seed_type": normalized_source, "priority": 1.25}
            for query in _broader_musicbrainz_query_seeds(
                req=req,
                profile=profile,
                taste=taste,
                limit=max(1, query_budget // 2) if query_budget else 0,
            )
        ]
        stored_seeds = collect_external_catalog_backfill_seeds(
            server,
            user_scope_id=scope,
            limit=max(query_budget * 2, 0),
        )
        result["stored_backfill_seeds"] = len(stored_seeds)
        enqueue_result = enqueue_external_catalog_seeds(
            server,
            [*user_seeds, *stored_seeds],
            user_scope_id=scope,
            provider="musicbrainz",
            source=normalized_source,
        )
        result["queued"] = int(enqueue_result.get("queued") or 0)
        if enqueue_result.get("error"):
            result["error"] = _text(enqueue_result.get("error"))[:240]

        while result["processed"] < query_budget:
            if time.perf_counter() >= deadline:
                result["status"] = "budget_exhausted"
                break
            remaining = query_budget - int(result["processed"] or 0) if query_budget else import_batch_size
            import_result = run_external_catalog_import(
                server,
                user_scope_id=scope,
                provider="musicbrainz",
                batch_size=min(import_batch_size, max(remaining, 1)),
                musicbrainz_client=musicbrainz_client,
            )
            processed = int(import_result.get("processed") or 0)
            for key in ("processed", "imported", "completed", "no_results", "failed", "retry"):
                result[key] = int(result.get(key) or 0) + int(import_result.get(key) or 0)
            if import_result.get("error"):
                result["error"] = _text(import_result.get("error"))[:240]
                break
            if processed <= 0:
                break

        if time.perf_counter() < deadline:
            backfill = backfill_canonical_catalog(
                server,
                search_event_limit=240,
                canonical_entity_limit=240,
                musicbrainz_query_limit=0,
            )
            result["backfill_processed"] = int(backfill.get("stored_entities") or 0)
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)[:240]
    finally:
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        with _LOCK:
            _INFLIGHT.discard(fingerprint)
            _LAST_RUN[fingerprint] = time.time()
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
        provider = _text(payload.get("source_provider")).lower()
        if (
            provider == "musicbrainz"
            or track_id.startswith("musicbrainz:")
            or payload.get("playable") is False
        ):
            continue
        has_playable_source = bool(
            track_id
            and (
                payload.get("videoId")
                or payload.get("channel")
                or payload.get("channel_id")
                or payload.get("channelId")
                or payload.get("artist")
                or payload.get("artist_name")
                or provider in {"youtube", "ytmusic", "youtube_music", "search", "history"}
            )
        )
        if not has_playable_source:
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


def schedule_catalog_population(
    server: Any,
    *,
    user_scope_id: str,
    req: Any | None = None,
    profile: Any | None = None,
    taste: Any | None = None,
    reason: str = "background",
    run_musicbrainz: bool = False,
    min_interval_seconds: float = 60.0,
) -> Dict[str, Any]:
    tracks = collect_catalog_seed_tracks(req=req, profile=profile, taste=taste, limit=24)
    fingerprint_payload = {
        "scope": _text(user_scope_id) or "guest",
        "reason": reason,
        "tracks": [
            _track_id(track) or f"{normalize_track_title(_track_title(track))}|{normalize_artist_name(_track_artist(track))}"
            for track in tracks[:12]
        ],
        "musicbrainz": bool(run_musicbrainz),
    }
    fingerprint = hashlib.sha1(
        json.dumps(fingerprint_payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    now = time.time()
    with _LOCK:
        if fingerprint in _INFLIGHT:
            return {"scheduled": False, "reason": "inflight", "fingerprint": fingerprint}
        if now - _LAST_RUN.get(fingerprint, 0.0) < max(float(min_interval_seconds or 0.0), 0.0):
            return {"scheduled": False, "reason": "recent", "fingerprint": fingerprint}
        _INFLIGHT.add(fingerprint)

    def worker() -> None:
        try:
            if run_musicbrainz:
                run_catalog_warmup(
                    server,
                    user_scope_id=user_scope_id,
                    req=req,
                    profile=profile,
                    taste=taste,
                    max_queries=18,
                    batch_size=4,
                    time_budget_seconds=35.0,
                    min_interval_seconds=0.0,
                    force=True,
                    source=f"{reason}_catalog_seed",
                )
            else:
                populate_catalog_from_user_signals(
                    server,
                    user_scope_id=user_scope_id,
                    req=req,
                    profile=profile,
                    taste=taste,
                    run_musicbrainz=False,
                    source=f"{reason}_catalog_seed",
                )
        finally:
            with _LOCK:
                _INFLIGHT.discard(fingerprint)
                _LAST_RUN[fingerprint] = time.time()

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
