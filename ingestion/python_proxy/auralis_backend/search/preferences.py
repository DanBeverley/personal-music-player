"""Small, account-scoped memory for explicit web search selections."""
from __future__ import annotations

import json
import time
from copy import deepcopy
from difflib import SequenceMatcher
from typing import Any

from ..recommend.store_runtime import (
    init_recommendation_store,
    open_recommendation_store_connection,
)
from ..domain.catalog import normalize_catalog_text


SOURCE = "web_search_selection"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _query_key(query: str) -> str:
    return normalize_catalog_text(query)


def _is_title_related(query_key: str, title_key: str, artist_key: str) -> bool:
    if not query_key or not title_key or len(query_key) < 3:
        return False
    if query_key == title_key or query_key in title_key:
        return True
    if title_key in query_key:
        extra = set(query_key.replace(title_key, " ").split())
        # A query that adds an explicit different artist/version qualifier must
        # keep its intent and cannot be rewritten from a clicked result.
        return not extra or extra.issubset(set(artist_key.split()))
    return (len(query_key.split()) == len(title_key.split()) and
            SequenceMatcher(None, query_key, title_key).ratio() >= 0.72)


def remember_selected_query(server: Any, *, user_scope_id: str, query: str,
                            item: dict[str, Any]) -> bool:
    scope = _text(user_scope_id)
    query = _text(query)
    title = _text(item.get("title"))
    artist = _text(item.get("artist"))
    entity_key = _text(item.get("canonical_recording_id") or
                       item.get("recording_id") or item.get("id") or
                       item.get("track_key"))
    query_key = _query_key(query)
    title_key = _query_key(title)
    if (not scope or scope.lower() in {"guest", "anonymous", ""} or
            not query_key or not title_key or not entity_key or
            not artist or _query_key(artist) in {"unknown", "unknown artist"} or
            len(query_key) < 3):
        return False
    # Keep title-first searches only. Artist-only and clearly unrelated queries
    # must not be rewritten from a clicked result.
    if not _is_title_related(query_key, title_key, _query_key(artist)):
        return False
    # Never learn a title-only rewrite when the submitted text explicitly names
    # another artist; the returned query retains the selected artist as context.
    payload = json.dumps({"source": SOURCE, "title": title, "artist": artist,
                          "selected_id": _text(item.get("id") or item.get("track_key")),
                          "canonical_recording_id": entity_key}, ensure_ascii=False)
    init_recommendation_store(server)
    connection = open_recommendation_store_connection(server)
    try:
        connection.execute(
            """INSERT INTO search_query_memory(
                 user_scope_id, query_key, entity_type, entity_key, title_key,
                 artist_key, score, confidence, event_count, payload_json, updated_at
               ) VALUES (?, ?, 'track', ?, ?, ?, 1.0, 0.9, 1, ?, ?)
               ON CONFLICT(user_scope_id, query_key, entity_type, entity_key)
               DO UPDATE SET score=search_query_memory.score+1.0,
                 confidence=max(search_query_memory.confidence, 0.9),
                 event_count=search_query_memory.event_count+1,
                 payload_json=excluded.payload_json, updated_at=excluded.updated_at
            """, [scope, query_key, entity_key, title_key,
                  _query_key(artist), payload, time.time()])
        connection.commit()
        return True
    finally:
        connection.close()


def _load_selection(server: Any, user_scope_id: str, query: str) -> dict | None:
    scope = _text(user_scope_id)
    query = _text(query)
    query_key = _query_key(query)
    if not scope or scope.lower() in {"guest", "anonymous"} or not query_key:
        return None
    try:
        init_recommendation_store(server)
        connection = open_recommendation_store_connection(server)
        try:
            rows = connection.execute(
                """SELECT payload_json FROM search_query_memory
                   WHERE user_scope_id=? AND query_key=? AND entity_type='track'
                   ORDER BY score DESC, updated_at DESC LIMIT 8""", [scope, query_key]
            ).fetchall()
        finally:
            connection.close()
    except Exception:
        return None
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            continue
        if payload.get("source") != SOURCE:
            continue
        title, artist = _text(payload.get("title")), _text(payload.get("artist"))
        title_key = _query_key(title)
        if not title or not artist or len(query_key) < 3:
            continue
        if not _is_title_related(query_key, title_key, _query_key(artist)):
            continue
        # Explicit artist words survive in the rewrite, including artist-first
        # queries; the selected artist is always included for disambiguation.
        return payload
    return None


def load_selected_query(server: Any, user_scope_id: str, query: str) -> str | None:
    selected = _load_selection(server, user_scope_id, query)
    return f"{selected['title']} {selected['artist']}" if selected else None


def apply_selected_result(server: Any, user_scope_id: str, query: str, response: dict) -> dict:
    """Promote a still-retrievable selection only in this account's response."""
    selected = _load_selection(server, user_scope_id, query)
    if not selected:
        return response
    # Prefer the exact selected source; catalog recording keys can be shared
    # across renditions and must not outrank an available exact selection.
    exact_id = str(selected.get('selected_id') or '')
    identities = {exact_id or str(selected.get('canonical_recording_id') or '')} - {''}
    chosen = None
    for surface in ('tracks', 'results', 'artist_tracks'):
        for item in response.get(surface) or []:
            if not isinstance(item, dict):
                continue
            keys = {str(item.get(key) or '') for key in
                    ('id', 'videoId', 'track_key', 'canonical_recording_id', 'musicbrainz_recording_id')}
            if identities & keys and item.get('playable') is not False:
                chosen = item
                break
        if chosen:
            break
    if not chosen:
        return response
    result = deepcopy(response)
    for surface in ('tracks', 'results'):
        values = result.get(surface)
        if isinstance(values, list):
            result[surface] = [deepcopy(chosen), *[item for item in values if item != chosen]]
    result['top_result'] = {'entity_type': 'track', 'item': deepcopy(chosen)}
    album = result.get('containing_album') or {}
    if album and str(album.get('id') or album.get('browseId') or '') != str(chosen.get('album_id') or ''):
        result['containing_album'] = None
    return result
