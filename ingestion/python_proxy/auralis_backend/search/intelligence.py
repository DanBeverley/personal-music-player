from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Dict, Iterable, List

from ..domain.catalog import (
    catalog_source_authority,
    canonical_title_artist_identity,
    normalize_album_title,
    normalize_artist_name,
    normalize_track_title,
    normalized_popularity,
)
from ..recommend.store_runtime import open_recommendation_store_connection
from .canonical import canonical_track_fields, source_quality_score
from .musicbrainz import (
    search_musicbrainz_album_items,
    search_musicbrainz_artist_items,
    search_musicbrainz_recording_items,
)
from .server_adapter import SearchServerAdapter


GLOBAL_SEARCH_SCOPE = "__global__"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _json_loads(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def search_query_key(query: str) -> str:
    return normalize_track_title(query)


def _entity_display_fields(item: Dict[str, Any]) -> Dict[str, str]:
    title = _text(item.get("title") or item.get("name"))
    artist = _text(
        item.get("artist")
        or item.get("artist_name")
        or item.get("channel")
        or item.get("author")
        or item.get("uploader")
    )
    album = _text(item.get("album") or item.get("album_title") or item.get("album_name"))
    return {
        "display_title": title,
        "display_artist": artist,
        "display_album": album,
    }


def catalog_entity_key(entity_type: str, item: Dict[str, Any], *, query: str = "") -> str:
    normalized_type = _text(entity_type).lower() or "track"
    if normalized_type == "track":
        mbid = _text(item.get("musicbrainz_recording_id") or item.get("mb_recording_id"))
        if mbid:
            return f"musicbrainz:recording:{mbid}"
        title_key, artist_key, _album_key = canonical_track_fields(item, query=query)
        if title_key and artist_key:
            return f"{title_key}|{artist_key}"
        return title_key
    if normalized_type == "artist":
        mbid = _text(item.get("musicbrainz_artist_id") or item.get("mb_artist_id"))
        if mbid:
            return f"musicbrainz:artist:{mbid}"
        return normalize_artist_name(
            item.get("name")
            or item.get("artist")
            or item.get("artist_name")
            or item.get("channel")
            or item.get("author")
            or item.get("uploader")
        )
    if normalized_type == "album":
        mbid = _text(
            item.get("musicbrainz_release_group_id")
            or item.get("musicbrainz_release_id")
            or item.get("mb_release_group_id")
            or item.get("mb_release_id")
        )
        if mbid:
            return f"musicbrainz:release:{mbid}"
        album_key = normalize_album_title(
            item.get("album")
            or item.get("album_title")
            or item.get("album_name")
            or item.get("title")
            or item.get("name")
        )
        artist_key = normalize_artist_name(
            item.get("artist")
            or item.get("artist_name")
            or item.get("channel")
            or item.get("author")
            or item.get("uploader")
        )
        return f"{album_key}|{artist_key}" if album_key and artist_key else album_key
    return _text(item.get("id") or item.get("videoId") or item.get("browseId"))


def _source_provider(item: Dict[str, Any]) -> str:
    provider = _text(item.get("provider") or item.get("source_provider"))
    source = _text(item.get("source") or item.get("source_name"))
    combined = f"{provider} {source}".lower()
    if "youtube" in combined or "ytmusic" in combined:
        return "youtube"
    return provider.lower() or "ytmusic"


def _source_name(item: Dict[str, Any]) -> str:
    return _text(
        item.get("channel")
        or item.get("artist")
        or item.get("artist_name")
        or item.get("author")
        or item.get("uploader")
    )


def _source_key(item: Dict[str, Any]) -> str:
    explicit = _text(
        item.get("channel_id")
        or item.get("channelId")
        or item.get("uploader_id")
        or item.get("uploaderId")
        or item.get("artist_id")
        or item.get("artistId")
        or item.get("musicbrainz_recording_id")
        or item.get("musicbrainz_artist_id")
        or item.get("musicbrainz_release_group_id")
        or item.get("musicbrainz_release_id")
    )
    if explicit:
        return explicit
    name = _source_name(item)
    if not name:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _has_structured_source_identity(item: Dict[str, Any]) -> bool:
    return bool(
        _text(
            item.get("channel_id")
            or item.get("channelId")
            or item.get("uploader_id")
            or item.get("uploaderId")
            or item.get("artist_id")
            or item.get("artistId")
            or item.get("album_id")
            or item.get("albumId")
            or item.get("browseId")
            or item.get("musicbrainz_recording_id")
            or item.get("musicbrainz_artist_id")
            or item.get("musicbrainz_release_id")
            or item.get("musicbrainz_release_group_id")
        )
    )


def infer_source_identity(item: Dict[str, Any]) -> Dict[str, Any]:
    """Infer provider/channel authority from structured source evidence.

    This intentionally avoids hardcoded artist names. It caches source classes
    such as Topic, VEVO, official artist channel, and album-linked catalog item.
    """
    if not isinstance(item, dict):
        return {}
    provider = _source_provider(item)
    key = _source_key(item)
    name = _source_name(item)
    if not key and not name:
        return {}
    text = " ".join(
        _text(item.get(field))
        for field in (
            "channel",
            "artist",
            "artist_name",
            "author",
            "uploader",
            "description",
            "source",
            "source_name",
        )
    ).lower()
    structured = _has_structured_source_identity(item)
    authority = "user_upload"
    confidence = 0.25 if structured else 0.1
    if provider == "musicbrainz" or _text(item.get("metadata_source")) == "musicbrainz":
        authority = "verified_catalog"
        confidence = 0.96
    elif "vevo" in text:
        authority = "vevo"
        confidence = 0.92 if structured else 0.72
    elif re.search(r"(^|\s|-)topic($|\s)", text) or text.endswith("- topic"):
        authority = "topic"
        confidence = 0.9 if structured else 0.72
    elif "provided to youtube" in text or "auto generated by youtube" in text or "auto-generated by youtube" in text:
        authority = "topic"
        confidence = 0.86
    elif _text(item.get("album_id") or item.get("albumId")) and _text(
        item.get("album") or item.get("album_title") or item.get("album_name")
    ):
        authority = "album_relation"
        confidence = 0.78
    elif "official" in text and structured:
        authority = "official_artist_channel"
        confidence = 0.76
    explicit = catalog_source_authority(item)
    if explicit in {"official", "canonical", "verified_catalog"}:
        authority = "verified_catalog" if authority == "user_upload" else authority
        confidence = max(confidence, 0.74)
    return {
        "source_provider": provider,
        "source_key": key or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
        "source_name": name,
        "authority": authority,
        "confidence": round(confidence, 4),
        "evidence": {
            "structured_source": structured,
            "album_id": _text(item.get("album_id") or item.get("albumId")),
            "browse_id": _text(item.get("browseId")),
            "source_authority": explicit,
        },
    }


def _identity_source_authority(authority: str, confidence: float) -> str:
    normalized = _text(authority).lower()
    if normalized in {"vevo", "topic", "official_artist_channel"} and confidence >= 0.68:
        return "official"
    if normalized in {"album_relation", "verified_catalog"} and confidence >= 0.7:
        return "verified_catalog"
    return ""


def _identity_payload_for_item(item: Dict[str, Any]) -> Dict[str, Any]:
    identity = infer_source_identity(item)
    return identity if identity else {}


def _event_popularity_delta(event_type: str, event_weight: float) -> float:
    normalized = _text(event_type).lower()
    weight = max(float(event_weight or 0.0), 0.0)
    if normalized in {"play_start", "play", "playback_start", "queue_play"}:
        return 0.06 + (weight * 0.025)
    if normalized in {"detail_open", "artist_open", "album_open", "playlist_open", "click", "tap"}:
        return 0.035 + (weight * 0.015)
    if normalized in {"add_to_playlist", "save", "like"}:
        return 0.08 + (weight * 0.03)
    if normalized in {"skip", "hide", "dislike"}:
        return -0.08
    return 0.02 + (weight * 0.01)


def _event_counter_columns(event_type: str) -> tuple[int, int, int]:
    normalized = _text(event_type).lower()
    if normalized in {"play_start", "play", "playback_start", "queue_play"}:
        return (0, 1, 0)
    if normalized in {"skip", "hide", "dislike"}:
        return (0, 0, 1)
    return (1, 0, 0)


def _official_source_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    identity = _identity_payload_for_item(item)
    authority = _text(identity.get("authority"))
    confidence = float(identity.get("confidence") or 0.0)
    if not authority or confidence < 0.55:
        return {
            "official_source_provider": "",
            "official_source_key": "",
            "official_source_authority": "",
            "official_confidence": 0.0,
        }
    return {
        "official_source_provider": _text(identity.get("source_provider")),
        "official_source_key": _text(identity.get("source_key")),
        "official_source_authority": authority,
        "official_confidence": confidence,
    }


def load_source_identity(server: Any, item: Dict[str, Any]) -> Dict[str, Any]:
    inferred = infer_source_identity(item)
    provider = _text(inferred.get("source_provider"))
    source_key = _text(inferred.get("source_key"))
    if not provider or not source_key:
        return inferred
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return inferred
    try:
        row = connection.execute(
            """
            SELECT source_provider, source_key, source_name, authority,
                   confidence, evidence_json, updated_at
            FROM search_source_identities
            WHERE source_provider = ? AND source_key = ?
            """,
            [provider, source_key],
        ).fetchone()
    except Exception:
        return inferred
    finally:
        connection.close()
    if not row:
        return inferred
    cached = {
        "source_provider": row["source_provider"],
        "source_key": row["source_key"],
        "source_name": row["source_name"],
        "authority": row["authority"],
        "confidence": float(row["confidence"] or 0.0),
        "evidence": _json_loads(row["evidence_json"]),
        "cached": True,
    }
    if float(inferred.get("confidence") or 0.0) > cached["confidence"]:
        return inferred
    return cached


def annotate_source_identity(server: Any, item: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    annotated = dict(item)
    identity = load_source_identity(server, annotated)
    if not identity:
        return annotated
    annotated["source_identity_provider"] = identity.get("source_provider") or ""
    annotated["source_identity_key"] = identity.get("source_key") or ""
    annotated["source_identity_name"] = identity.get("source_name") or ""
    annotated["source_identity_authority"] = identity.get("authority") or ""
    annotated["source_identity_confidence"] = identity.get("confidence") or 0.0
    mapped_authority = _identity_source_authority(
        _text(identity.get("authority")),
        float(identity.get("confidence") or 0.0),
    )
    if mapped_authority and _text(annotated.get("source_authority")) in {"", "unknown"}:
        annotated["source_authority"] = mapped_authority
    return annotated


def annotate_source_identities(server: Any, items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [annotate_canonical_entity(server, item) for item in items or [] if isinstance(item, dict)]


def _entity_key(entity_type: str, item: Dict[str, Any]) -> str:
    explicit = _text(
        item.get("canonical_title_artist_identity")
        or item.get("canonical_track_identity")
        or item.get("canonical_source_identity")
    )
    if explicit:
        return explicit
    if entity_type == "track":
        mbid = _text(item.get("musicbrainz_recording_id") or item.get("mb_recording_id"))
        if mbid:
            return f"musicbrainz:recording:{mbid}"
    elif entity_type == "artist":
        mbid = _text(item.get("musicbrainz_artist_id") or item.get("mb_artist_id"))
        if mbid:
            return f"musicbrainz:artist:{mbid}"
    elif entity_type == "album":
        mbid = _text(
            item.get("musicbrainz_release_group_id")
            or item.get("musicbrainz_release_id")
            or item.get("mb_release_group_id")
            or item.get("mb_release_id")
        )
        if mbid:
            return f"musicbrainz:release:{mbid}"
    if entity_type == "track":
        return canonical_title_artist_identity(item)
    title_key, artist_key, _album_key = canonical_track_fields(item)
    if title_key and artist_key:
        return f"{title_key}|{artist_key}"
    return _text(item.get("id") or item.get("videoId") or item.get("browseId"))


def load_canonical_entity(
    server: Any,
    *,
    entity_type: str,
    item: Dict[str, Any],
) -> Dict[str, Any]:
    normalized_type = _text(entity_type) or "track"
    key = _entity_key(normalized_type, item)
    if not key:
        return {}
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return {}
    try:
        row = connection.execute(
            """
            SELECT entity_type, entity_key, title_key, artist_key, album_key,
                   source_authority, source_quality, popularity,
                   click_count, play_count, skip_count, payload_json,
                   official_source_provider, official_source_key,
                   official_source_authority, official_confidence,
                   learned_popularity, updated_at
            FROM search_canonical_entities
            WHERE entity_type = ? AND entity_key = ?
            """,
            [normalized_type, key],
        ).fetchone()
    except Exception:
        return {}
    finally:
        connection.close()
    if not row:
        return {}
    return {
        "entity_type": row["entity_type"],
        "entity_key": row["entity_key"],
        "title_key": row["title_key"],
        "artist_key": row["artist_key"],
        "album_key": row["album_key"],
        "source_authority": row["source_authority"],
        "source_quality": float(row["source_quality"] or 0.0),
        "popularity": float(row["popularity"] or 0.0),
        "click_count": int(row["click_count"] or 0),
        "play_count": int(row["play_count"] or 0),
        "skip_count": int(row["skip_count"] or 0),
        "payload": _json_loads(row["payload_json"]),
        "official_source_provider": row["official_source_provider"],
        "official_source_key": row["official_source_key"],
        "official_source_authority": row["official_source_authority"],
        "official_confidence": float(row["official_confidence"] or 0.0),
        "learned_popularity": float(row["learned_popularity"] or 0.0),
        "updated_at": float(row["updated_at"] or 0.0),
    }


def annotate_canonical_entity(server: Any, item: Dict[str, Any]) -> Dict[str, Any]:
    annotated = annotate_source_identity(server, item)
    entity = load_canonical_entity(server, entity_type="track", item=annotated)
    if not entity:
        return annotated
    annotated["canonical_registry_entity_key"] = entity.get("entity_key") or ""
    annotated["learned_popularity"] = entity.get("learned_popularity") or 0.0
    annotated["search_click_count"] = entity.get("click_count") or 0
    annotated["search_play_count"] = entity.get("play_count") or 0
    annotated["search_skip_count"] = entity.get("skip_count") or 0
    annotated["official_source_provider"] = entity.get("official_source_provider") or ""
    annotated["official_source_key"] = entity.get("official_source_key") or ""
    annotated["official_source_authority"] = entity.get("official_source_authority") or ""
    annotated["official_confidence"] = entity.get("official_confidence") or 0.0
    if (
        not _text(annotated.get("source_identity_authority"))
        and _text(entity.get("official_source_authority"))
    ):
        annotated["source_identity_authority"] = entity.get("official_source_authority")
        annotated["source_identity_confidence"] = entity.get("official_confidence") or 0.0
    mapped_authority = _identity_source_authority(
        _text(annotated.get("source_identity_authority")),
        float(annotated.get("source_identity_confidence") or 0.0),
    )
    if mapped_authority and _text(annotated.get("source_authority")) in {"", "unknown"}:
        annotated["source_authority"] = mapped_authority
    return annotated


def load_query_memory(
    server: Any,
    *,
    user_scope_id: str,
    query: str,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    query_key = search_query_key(query)
    if not query_key:
        return []
    scopes = [GLOBAL_SEARCH_SCOPE]
    normalized_scope = _text(user_scope_id) or "guest"
    if normalized_scope and normalized_scope != "guest":
        scopes.insert(0, normalized_scope)
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return []
    try:
        placeholders = ",".join("?" for _ in scopes)
        rows = connection.execute(
            f"""
            SELECT user_scope_id, entity_type, entity_key, title_key, artist_key,
                   score, confidence, event_count, payload_json, updated_at
            FROM search_query_memory
            WHERE query_key = ? AND user_scope_id IN ({placeholders})
            ORDER BY score DESC, confidence DESC, updated_at DESC
            LIMIT ?
            """,
            [query_key, *scopes, max(int(limit or 0), 1)],
        ).fetchall()
    except Exception:
        return []
    finally:
        connection.close()
    output: List[Dict[str, Any]] = []
    for row in rows:
        output.append(
            {
                "user_scope_id": row["user_scope_id"],
                "entity_type": row["entity_type"],
                "entity_key": row["entity_key"],
                "title_key": row["title_key"],
                "artist_key": row["artist_key"],
                "score": float(row["score"] or 0.0),
                "confidence": float(row["confidence"] or 0.0),
                "event_count": int(row["event_count"] or 0),
                "payload": _json_loads(row["payload_json"]),
                "updated_at": float(row["updated_at"] or 0.0),
            }
        )
    return output


def load_query_aliases(
    server: Any,
    *,
    query: str,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    alias_key = search_query_key(query)
    if not alias_key:
        return []
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return []
    try:
        rows = connection.execute(
            """
            SELECT canonical_query_key, entity_type, entity_key, title_key,
                   artist_key, score, confidence, event_count, source,
                   payload_json, updated_at
            FROM search_query_aliases
            WHERE alias_key = ?
            ORDER BY score DESC, confidence DESC, updated_at DESC
            LIMIT ?
            """,
            [alias_key, max(int(limit or 0), 1)],
        ).fetchall()
    except Exception:
        return []
    finally:
        connection.close()
    aliases: List[Dict[str, Any]] = []
    for row in rows:
        aliases.append(
            {
                "user_scope_id": GLOBAL_SEARCH_SCOPE,
                "entity_type": row["entity_type"],
                "entity_key": row["entity_key"],
                "title_key": row["title_key"],
                "artist_key": row["artist_key"],
                "score": float(row["score"] or 0.0),
                "confidence": float(row["confidence"] or 0.0),
                "event_count": int(row["event_count"] or 0),
                "source": row["source"],
                "canonical_query_key": row["canonical_query_key"],
                "payload": _json_loads(row["payload_json"]),
                "updated_at": float(row["updated_at"] or 0.0),
            }
        )
    return aliases


def load_catalog_entity_memories(
    server: Any,
    *,
    query: str,
    entity_type: str = "track",
    limit: int = 6,
) -> List[Dict[str, Any]]:
    alias_key = search_query_key(query)
    normalized_type = _text(entity_type) or "track"
    if not alias_key:
        return []
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return []
    try:
        rows = connection.execute(
            """
            SELECT a.entity_type, a.entity_key, a.score, a.confidence,
                   e.display_title, e.display_artist, e.display_album,
                   e.learned_popularity, e.popularity, e.payload_json, e.updated_at
            FROM catalog_entity_aliases a
            JOIN catalog_entities e
              ON e.entity_type = a.entity_type AND e.entity_key = a.entity_key
            WHERE a.alias_key = ? AND a.entity_type = ?
            ORDER BY a.score DESC, a.confidence DESC,
                     e.learned_popularity DESC, e.popularity DESC, a.updated_at DESC
            LIMIT ?
            """,
            [alias_key, normalized_type, max(int(limit or 0), 1)],
        ).fetchall()
    except Exception:
        return []
    finally:
        connection.close()
    memories: List[Dict[str, Any]] = []
    for row in rows:
        payload = _json_loads(row["payload_json"])
        title_key, artist_key, _album_key = canonical_track_fields(payload, query=query)
        if not title_key:
            title_key = normalize_track_title(row["display_title"])
        if not artist_key:
            artist_key = normalize_artist_name(row["display_artist"])
        memories.append(
            {
                "user_scope_id": GLOBAL_SEARCH_SCOPE,
                "entity_type": row["entity_type"],
                "entity_key": row["entity_key"],
                "title_key": title_key,
                "artist_key": artist_key,
                "score": float(row["score"] or 0.0)
                + (float(row["learned_popularity"] or 0.0) * 2.0)
                + (float(row["popularity"] or 0.0) * 0.8),
                "confidence": float(row["confidence"] or 0.0),
                "event_count": 1,
                "source": "catalog_entity_alias",
                "payload": payload,
                "updated_at": float(row["updated_at"] or 0.0),
            }
        )
    return memories


def remember_candidate_observations(
    server: Any,
    *,
    user_scope_id: str,
    query: str,
    items: Iterable[Dict[str, Any]],
    entity_type: str = "track",
    confidence_floor: float = 0.72,
    limit: int = 8,
) -> int:
    """Learn only trusted candidate evidence from search result sets.

    Displaying a result is not the same as the user choosing it. This keeps
    weak/ambiguous candidates out of the canonical graph while still letting
    official Topic/VEVO/catalog-like candidates improve future edge queries.
    """
    query_key = search_query_key(query)
    if not query_key:
        return 0
    adapter = SearchServerAdapter(server)
    stored = 0
    seen: set[str] = set()
    for raw in items or []:
        if stored >= max(int(limit or 0), 1):
            break
        if not isinstance(raw, dict):
            continue
        item = annotate_source_identity(server, raw)
        normalized_type = _text(entity_type) or "track"
        entity_key = catalog_entity_key(normalized_type, item, query=query)
        if not entity_key or entity_key in seen:
            continue
        title_key, artist_key, album_key = canonical_track_fields(item, query=query)
        if normalized_type == "track":
            if not title_key:
                continue
            exact_title = title_key == query_key
            contained_title = query_key in title_key or title_key in query_key
            if not exact_title and not contained_title:
                continue
        source_quality = source_quality_score(adapter, item)
        identity_confidence = 0.0
        try:
            identity_confidence = float(item.get("source_identity_confidence") or 0.0)
        except (TypeError, ValueError):
            identity_confidence = 0.0
        source_authority = _text(
            item.get("source_identity_authority") or catalog_source_authority(item)
        ).lower()
        popularity = normalized_popularity(item)
        trusted_source = (
            source_quality >= 0.72
            or identity_confidence >= 0.72
            or source_authority in {
                "vevo",
                "topic",
                "official_artist_channel",
                "album_relation",
                "verified_catalog",
                "canonical",
                "official",
            }
        )
        if not trusted_source:
            continue
        confidence = min(
            0.96,
            0.42
            + max(source_quality, 0.0) * 0.22
            + identity_confidence * 0.22
            + popularity * 0.14,
        )
        if confidence < confidence_floor:
            continue
        seen.add(entity_key)
        if remember_catalog_entity(
            server,
            user_scope_id=user_scope_id,
            query=query,
            entity_type=normalized_type,
            item=item,
            confidence=confidence,
            event_weight=0.25,
            event_type="candidate_observation",
            source="trusted_search_candidate",
        ):
            stored += 1
    return stored


def enrich_query_with_musicbrainz(
    server: Any,
    *,
    user_scope_id: str,
    query: str,
    limit: int = 5,
    client: Any | None = None,
) -> Dict[str, Any]:
    """Import trusted MusicBrainz metadata for a query into canonical memory.

    This is a bounded enrichment step. It never returns playable tracks by
    itself; it seeds canonical title/artist/album identity so the existing
    YouTube/YTMusic candidate ranker can choose the right playable source.
    """
    query_key = search_query_key(query)
    result: Dict[str, Any] = {
        "attempted": False,
        "imported": 0,
        "imported_tracks": 0,
        "imported_artists": 0,
        "imported_albums": 0,
        "candidate_count": 0,
        "track_candidate_count": 0,
        "artist_candidate_count": 0,
        "album_candidate_count": 0,
        "error": "",
    }
    if not query_key:
        return result
    existing_tracks = load_catalog_entity_memories(
        server,
        query=query,
        entity_type="track",
        limit=1,
    )
    existing_artists = load_catalog_entity_memories(
        server,
        query=query,
        entity_type="artist",
        limit=1,
    )
    existing_albums = load_catalog_entity_memories(
        server,
        query=query,
        entity_type="album",
        limit=1,
    )
    if existing_tracks and existing_artists and existing_albums:
        result["candidate_count"] = (
            len(existing_tracks) + len(existing_artists) + len(existing_albums)
        )
        return result
    result["attempted"] = True
    try:
        track_items = search_musicbrainz_recording_items(
            query,
            client=client,
            limit=max(1, min(int(limit or 1), 8)),
        )
        artist_items = search_musicbrainz_artist_items(
            query,
            client=client,
            limit=max(1, min(int(limit or 1), 5)),
        )
        album_items = search_musicbrainz_album_items(
            query,
            client=client,
            limit=max(1, min(int(limit or 1), 5)),
        )
    except Exception as exc:
        result["error"] = str(exc)[:240]
        return result
    result["track_candidate_count"] = len(track_items)
    result["artist_candidate_count"] = len(artist_items)
    result["album_candidate_count"] = len(album_items)
    result["candidate_count"] = len(track_items) + len(artist_items) + len(album_items)

    def import_item(item: Dict[str, Any], *, entity_type: str) -> bool:
        try:
            score = float(item.get("musicbrainz_score") or item.get("popularity") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        confidence = max(0.72, min(0.98, 0.72 + (score * 0.24)))
        return remember_catalog_entity(
            server,
            user_scope_id=user_scope_id or "guest",
            query=query,
            entity_type=entity_type,
            item=item,
            confidence=confidence,
            event_weight=max(0.2, score),
            event_type="musicbrainz_import",
            source="musicbrainz",
        )

    for item in track_items:
        if import_item(item, entity_type="track"):
            result["imported"] += 1
            result["imported_tracks"] += 1
    for item in artist_items:
        if import_item(item, entity_type="artist"):
            result["imported"] += 1
            result["imported_artists"] += 1
    for item in album_items:
        if import_item(item, entity_type="album"):
            result["imported"] += 1
            result["imported_albums"] += 1
    return result


def _search_event_selected_item(payload: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
    selected_item: Dict[str, Any] = {}
    for key in (
        "selected_item",
        "clicked_item",
        "track",
        "item",
        "entity",
    ):
        value = payload.get(key)
        if isinstance(value, dict):
            selected_item = dict(value)
            break
    selected_type = _text(
        payload.get("selected_entity_type")
        or payload.get("clicked_entity_type")
        or payload.get("entity_type")
        or ("track" if selected_item else "")
    )
    return selected_item, selected_type or "track"


def _backfill_seen_keys(server: Any, keys: Iterable[str]) -> set[str]:
    key_list = [_text(key) for key in keys if _text(key)]
    if not key_list:
        return set()
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return set()
    try:
        seen: set[str] = set()
        for index in range(0, len(key_list), 128):
            chunk = key_list[index : index + 128]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""
                SELECT event_key
                FROM search_catalog_backfill_events
                WHERE event_key IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            seen.update(_text(row["event_key"]) for row in rows)
        return seen
    except Exception:
        return set()
    finally:
        connection.close()


def _mark_backfill_events(server: Any, rows: Iterable[tuple[str, str]]) -> int:
    payload = [
        (_text(event_key), _text(source) or "catalog_backfill", time.time())
        for event_key, source in rows
        if _text(event_key)
    ]
    if not payload:
        return 0
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return 0
    try:
        connection.executemany(
            """
            INSERT OR IGNORE INTO search_catalog_backfill_events(
                event_key, source, processed_at
            )
            VALUES (?, ?, ?)
            """,
            payload,
        )
        connection.commit()
        return int(connection.total_changes or 0)
    except Exception:
        return 0
    finally:
        connection.close()


def _stable_backfill_key(prefix: str, values: Dict[str, Any]) -> str:
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True)
    return f"{prefix}:{hashlib.sha1(raw.encode('utf-8')).hexdigest()}"


def backfill_canonical_catalog(
    server: Any,
    *,
    search_event_limit: int = 150,
    canonical_entity_limit: int = 150,
    musicbrainz_query_limit: int = 4,
) -> Dict[str, Any]:
    """Backfill the canonical catalog from stored trusted evidence.

    This is intentionally offline/maintenance-oriented. Live search keeps using
    the direct path; this worker converts past selections and trusted canonical
    rows into stable aliases, source links, and popularity signals.
    """
    result: Dict[str, Any] = {
        "processed_search_events": 0,
        "processed_canonical_entities": 0,
        "processed_musicbrainz_queries": 0,
        "stored_entities": 0,
        "stored_source_identities": 0,
        "marked": 0,
        "error": "",
    }
    try:
        connection = open_recommendation_store_connection(server)
    except Exception as exc:
        result["error"] = str(exc)[:240]
        return result
    try:
        search_rows = connection.execute(
            """
            SELECT id, user_scope_id, query, source, metadata_json, occurred_at
            FROM recommendation_search_events
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            [max(int(search_event_limit or 0), 1)],
        ).fetchall()
        canonical_rows = connection.execute(
            """
            SELECT entity_type, entity_key, title_key, artist_key, album_key,
                   source_authority, source_quality, popularity,
                   click_count, play_count, skip_count, payload_json,
                   official_source_authority, official_confidence,
                   learned_popularity, updated_at
            FROM search_canonical_entities
            WHERE payload_json IS NOT NULL
              AND payload_json != '{}'
              AND (
                    official_confidence >= 0.68
                 OR source_quality >= 0.72
                 OR play_count > 0
                 OR click_count > 1
                 OR learned_popularity >= 0.08
              )
            ORDER BY learned_popularity DESC, play_count DESC,
                     click_count DESC, official_confidence DESC, updated_at DESC
            LIMIT ?
            """,
            [max(int(canonical_entity_limit or 0), 1)],
        ).fetchall()
    except Exception as exc:
        connection.close()
        result["error"] = str(exc)[:240]
        return result
    finally:
        try:
            connection.close()
        except Exception:
            pass

    search_candidates: List[Dict[str, Any]] = []
    musicbrainz_candidates: List[Dict[str, Any]] = []
    seen_musicbrainz_query_keys: set[str] = set()
    for row in search_rows:
        payload = _json_loads(row["metadata_json"])
        item, entity_type = _search_event_selected_item(payload)
        if not item:
            query = _text(row["query"])
            query_key = search_query_key(query)
            if (
                query
                and query_key
                and query_key not in seen_musicbrainz_query_keys
                and len(musicbrainz_candidates) < max(int(musicbrainz_query_limit or 0), 0)
            ):
                seen_musicbrainz_query_keys.add(query_key)
                musicbrainz_candidates.append(
                    {
                        "event_key": _stable_backfill_key(
                            "musicbrainz_query",
                            {"query_key": query_key},
                        ),
                        "source": "musicbrainz_query_backfill",
                        "user_scope_id": row["user_scope_id"] or "guest",
                        "query": query,
                    }
                )
            continue
        event_key = f"search_event:{row['id']}"
        search_candidates.append(
            {
                "event_key": event_key,
                "source": "recommendation_search_events",
                "user_scope_id": row["user_scope_id"] or "guest",
                "query": row["query"] or "",
                "entity_type": entity_type,
                "item": item,
                "confidence": max(0.62, min(float(payload.get("confidence") or 0.82), 0.98)),
                "event_weight": max(1.0, float(payload.get("event_weight") or 1.0)),
                "event_type": _text(payload.get("event_type") or row["source"]) or "search_interaction",
            }
        )

    canonical_candidates: List[Dict[str, Any]] = []
    for row in canonical_rows:
        payload = _json_loads(row["payload_json"])
        if not payload:
            continue
        event_key = f"canonical_entity:{row['entity_type']}:{row['entity_key']}"
        confidence = max(
            float(row["official_confidence"] or 0.0),
            min(float(row["source_quality"] or 0.0), 1.0),
            min(1.0, 0.52 + float(row["learned_popularity"] or 0.0)),
        )
        canonical_candidates.append(
            {
                "event_key": event_key,
                "source": "search_canonical_entities",
                "user_scope_id": GLOBAL_SEARCH_SCOPE,
                "query": row["title_key"] or payload.get("title") or "",
                "entity_type": row["entity_type"] or "track",
                "item": payload,
                "confidence": max(0.62, min(confidence, 0.98)),
                "event_weight": max(
                    0.5,
                    float(row["learned_popularity"] or 0.0)
                    + (float(row["play_count"] or 0) * 0.12)
                    + (float(row["click_count"] or 0) * 0.05),
                ),
                "event_type": "canonical_backfill",
            }
        )

    all_keys = [
        candidate["event_key"]
        for candidate in search_candidates + canonical_candidates + musicbrainz_candidates
    ]
    seen_keys = _backfill_seen_keys(server, all_keys)
    marked_rows: List[tuple[str, str]] = []

    for candidate in musicbrainz_candidates:
        if candidate["event_key"] in seen_keys:
            continue
        enrichment = enrich_query_with_musicbrainz(
            server,
            user_scope_id=candidate["user_scope_id"],
            query=candidate["query"],
            limit=5,
        )
        if enrichment.get("imported") or enrichment.get("candidate_count"):
            result["processed_musicbrainz_queries"] += 1
            result["stored_entities"] += int(enrichment.get("imported") or 0)
            marked_rows.append((candidate["event_key"], candidate["source"]))

    for candidate in search_candidates:
        if candidate["event_key"] in seen_keys:
            continue
        if remember_search_resolution(
            server,
            user_scope_id=candidate["user_scope_id"],
            query=candidate["query"],
            entity_type=candidate["entity_type"],
            item=candidate["item"],
            confidence=candidate["confidence"],
            event_weight=candidate["event_weight"],
            event_type=candidate["event_type"],
            source="catalog_backfill_search_event",
        ):
            result["processed_search_events"] += 1
            result["stored_entities"] += 1
            if remember_source_identity(server, candidate["item"], confidence_floor=0.55):
                result["stored_source_identities"] += 1
            marked_rows.append((candidate["event_key"], candidate["source"]))

    for candidate in canonical_candidates:
        if candidate["event_key"] in seen_keys:
            continue
        if remember_catalog_entity(
            server,
            user_scope_id=candidate["user_scope_id"],
            query=candidate["query"],
            entity_type=candidate["entity_type"],
            item=candidate["item"],
            confidence=candidate["confidence"],
            event_weight=candidate["event_weight"],
            event_type=candidate["event_type"],
            source="canonical_catalog_backfill",
        ):
            result["processed_canonical_entities"] += 1
            result["stored_entities"] += 1
            if remember_source_identity(server, candidate["item"], confidence_floor=0.55):
                result["stored_source_identities"] += 1
            marked_rows.append((candidate["event_key"], candidate["source"]))

    result["marked"] = _mark_backfill_events(server, marked_rows)
    return result


def _memory_scopes(user_scope_id: str) -> List[str]:
    normalized_scope = _text(user_scope_id) or "guest"
    if normalized_scope and normalized_scope != "guest":
        return [normalized_scope, GLOBAL_SEARCH_SCOPE]
    return [GLOBAL_SEARCH_SCOPE]


def _alias_keys_for_entity(
    query_key: str,
    *,
    title_key: str,
    artist_key: str,
) -> List[str]:
    aliases: List[str] = []

    def add(value: str) -> None:
        key = search_query_key(value)
        if key and key not in aliases:
            aliases.append(key)

    add(query_key)
    if title_key:
        add(title_key)
    if title_key and artist_key:
        add(f"{artist_key} {title_key}")
        add(f"{title_key} {artist_key}")
        compact_title = re.sub(r"\b(the|and|n|of|a|an|to)\b", " ", title_key)
        compact_title = re.sub(r"\s+", " ", compact_title).strip()
        if compact_title and compact_title != title_key:
            add(compact_title)
            add(f"{artist_key} {compact_title}")
            add(f"{compact_title} {artist_key}")
        compact_artist = re.sub(r"\b(the|and|n|of|a|an)\b", " ", artist_key)
        compact_artist = re.sub(r"\s+", " ", compact_artist).strip()
        if compact_artist and compact_artist != artist_key:
            add(f"{compact_artist} {title_key}")
            add(f"{title_key} {compact_artist}")
            if compact_title and compact_title != title_key:
                add(f"{compact_artist} {compact_title}")
                add(f"{compact_title} {compact_artist}")
        initials = "".join(
            token[0]
            for token in artist_key.split()
            if token and token not in {"the", "and", "of", "a", "an"}
        )
        if len(initials) >= 2:
            add(f"{initials} {title_key}")
            add(f"{title_key} {initials}")
    return aliases


def _catalog_alias_keys(
    query_key: str,
    *,
    entity_type: str,
    item: Dict[str, Any],
    title_key: str,
    artist_key: str,
    album_key: str,
) -> List[str]:
    normalized_type = _text(entity_type) or "track"
    aliases: List[str] = []

    def add(value: str) -> None:
        key = search_query_key(value)
        if key and key not in aliases:
            aliases.append(key)

    if normalized_type == "track":
        for key in _alias_keys_for_entity(query_key, title_key=title_key, artist_key=artist_key):
            add(key)
    elif normalized_type == "artist":
        add(query_key)
        add(artist_key)
        add(_text(item.get("name") or item.get("artist") or item.get("channel")))
    elif normalized_type == "album":
        add(query_key)
        add(album_key)
        if album_key and artist_key:
            add(f"{artist_key} {album_key}")
            add(f"{album_key} {artist_key}")
    else:
        add(query_key)
    raw_aliases = item.get("aliases") or item.get("search_aliases") or []
    if isinstance(raw_aliases, (list, tuple, set)):
        for alias in raw_aliases:
            add(_text(alias))
    elif isinstance(raw_aliases, str):
        add(raw_aliases)
    return aliases


def remember_catalog_entity(
    server: Any,
    *,
    user_scope_id: str,
    query: str,
    entity_type: str,
    item: Dict[str, Any],
    confidence: float,
    event_weight: float = 1.0,
    event_type: str = "",
    source: str = "search_catalog",
) -> bool:
    if not isinstance(item, dict):
        return False
    normalized_type = _text(entity_type) or "track"
    query_key = search_query_key(query)
    entity_key = catalog_entity_key(normalized_type, item, query=query)
    if not entity_key:
        return False
    title_key, artist_key, album_key = canonical_track_fields(item, query=query)
    if normalized_type == "artist" and not artist_key:
        artist_key = catalog_entity_key("artist", item, query=query)
    if normalized_type == "album" and not album_key:
        album_key = normalize_album_title(item.get("title") or item.get("name") or item.get("album"))
    safe_confidence = max(0.0, min(float(confidence or 0.0), 1.0))
    weight = max(float(event_weight or 0.0), 0.0)
    popularity_delta = _event_popularity_delta(event_type or source, weight)
    display = _entity_display_fields(item)
    identity = infer_source_identity(item)
    alias_keys = _catalog_alias_keys(
        query_key,
        entity_type=normalized_type,
        item=item,
        title_key=title_key,
        artist_key=artist_key,
        album_key=album_key,
    )
    now = time.time()
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return False
    try:
        connection.execute(
            """
            INSERT INTO catalog_entities(
                entity_type, entity_key, display_title, display_artist, display_album,
                confidence, popularity, learned_popularity, payload_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_type, entity_key) DO UPDATE SET
                display_title = CASE
                    WHEN excluded.confidence >= catalog_entities.confidence
                    THEN excluded.display_title
                    ELSE catalog_entities.display_title
                END,
                display_artist = CASE
                    WHEN excluded.confidence >= catalog_entities.confidence
                    THEN excluded.display_artist
                    ELSE catalog_entities.display_artist
                END,
                display_album = CASE
                    WHEN excluded.confidence >= catalog_entities.confidence
                    THEN excluded.display_album
                    ELSE catalog_entities.display_album
                END,
                confidence = max(catalog_entities.confidence, excluded.confidence),
                popularity = max(catalog_entities.popularity, excluded.popularity),
                learned_popularity = max(0, min(1.0, catalog_entities.learned_popularity + excluded.learned_popularity)),
                payload_json = CASE
                    WHEN excluded.confidence >= catalog_entities.confidence
                    THEN excluded.payload_json
                    ELSE catalog_entities.payload_json
                END,
                updated_at = excluded.updated_at
            """,
            [
                normalized_type,
                entity_key,
                display["display_title"],
                display["display_artist"],
                display["display_album"],
                safe_confidence,
                normalized_popularity(item),
                popularity_delta,
                _json_dumps(item),
                now,
            ],
        )
        for alias_key in alias_keys:
            connection.execute(
                """
                INSERT INTO catalog_entity_aliases(
                    alias_key, entity_type, entity_key, score, confidence, source, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(alias_key, entity_type, entity_key) DO UPDATE SET
                    score = catalog_entity_aliases.score + excluded.score,
                    confidence = max(catalog_entity_aliases.confidence, excluded.confidence),
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                [
                    alias_key,
                    normalized_type,
                    entity_key,
                    (safe_confidence * 1.8) + weight,
                    safe_confidence,
                    source,
                    now,
                ],
            )
        if identity:
            connection.execute(
                """
                INSERT INTO catalog_entity_sources(
                    entity_type, entity_key, source_provider, source_key,
                    source_authority, confidence, payload_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_type, entity_key, source_provider, source_key) DO UPDATE SET
                    source_authority = CASE
                        WHEN excluded.confidence >= catalog_entity_sources.confidence
                        THEN excluded.source_authority
                        ELSE catalog_entity_sources.source_authority
                    END,
                    confidence = max(catalog_entity_sources.confidence, excluded.confidence),
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                [
                    normalized_type,
                    entity_key,
                    _text(identity.get("source_provider")),
                    _text(identity.get("source_key")),
                    _text(identity.get("authority")),
                    float(identity.get("confidence") or 0.0),
                    _json_dumps(identity),
                    now,
                ],
            )
        metric_name = _text(event_type or source) or "catalog_resolution"
        connection.execute(
            """
            INSERT INTO catalog_entity_metrics(
                entity_type, entity_key, metric_name, score, event_count, updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(entity_type, entity_key, metric_name) DO UPDATE SET
                score = catalog_entity_metrics.score + excluded.score,
                event_count = catalog_entity_metrics.event_count + 1,
                updated_at = excluded.updated_at
            """,
            [normalized_type, entity_key, metric_name, popularity_delta, now],
        )
        connection.commit()
        return True
    except Exception:
        return False
    finally:
        connection.close()


def remember_source_identity(
    server: Any,
    item: Dict[str, Any],
    *,
    confidence_floor: float = 0.0,
) -> bool:
    identity = infer_source_identity(item)
    provider = _text(identity.get("source_provider"))
    source_key = _text(identity.get("source_key"))
    if not provider or not source_key:
        return False
    confidence = float(identity.get("confidence") or 0.0)
    if confidence < confidence_floor:
        return False
    now = time.time()
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return False
    try:
        connection.execute(
            """
            INSERT INTO search_source_identities(
                source_provider, source_key, source_name, authority,
                confidence, evidence_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_provider, source_key) DO UPDATE SET
                source_name = CASE
                    WHEN excluded.confidence >= search_source_identities.confidence
                    THEN excluded.source_name
                    ELSE search_source_identities.source_name
                END,
                authority = CASE
                    WHEN excluded.confidence >= search_source_identities.confidence
                    THEN excluded.authority
                    ELSE search_source_identities.authority
                END,
                confidence = max(search_source_identities.confidence, excluded.confidence),
                evidence_json = excluded.evidence_json,
                updated_at = excluded.updated_at
            """,
            [
                provider,
                source_key,
                _text(identity.get("source_name")),
                _text(identity.get("authority")),
                confidence,
                _json_dumps(identity.get("evidence")),
                now,
            ],
        )
        connection.commit()
        return True
    except Exception:
        return False
    finally:
        connection.close()


def remember_source_identities(
    server: Any,
    items: Iterable[Dict[str, Any]],
    *,
    confidence_floor: float = 0.55,
    limit: int = 24,
) -> int:
    identities: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        identity = infer_source_identity(item)
        provider = _text(identity.get("source_provider"))
        source_key = _text(identity.get("source_key"))
        confidence = float(identity.get("confidence") or 0.0)
        if not provider or not source_key or confidence < confidence_floor:
            continue
        identity_key = (provider, source_key)
        if identity_key in seen:
            continue
        seen.add(identity_key)
        identities.append(identity)
        if len(identities) >= max(int(limit or 0), 1):
            break
    if not identities:
        return 0
    now = time.time()
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return 0
    stored = 0
    try:
        for identity in identities:
            connection.execute(
                """
                INSERT INTO search_source_identities(
                    source_provider, source_key, source_name, authority,
                    confidence, evidence_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_provider, source_key) DO UPDATE SET
                    source_name = CASE
                        WHEN excluded.confidence >= search_source_identities.confidence
                        THEN excluded.source_name
                        ELSE search_source_identities.source_name
                    END,
                    authority = CASE
                        WHEN excluded.confidence >= search_source_identities.confidence
                        THEN excluded.authority
                        ELSE search_source_identities.authority
                    END,
                    confidence = max(search_source_identities.confidence, excluded.confidence),
                    evidence_json = excluded.evidence_json,
                    updated_at = excluded.updated_at
                """,
                [
                    _text(identity.get("source_provider")),
                    _text(identity.get("source_key")),
                    _text(identity.get("source_name")),
                    _text(identity.get("authority")),
                    float(identity.get("confidence") or 0.0),
                    _json_dumps(identity.get("evidence")),
                    now,
                ],
            )
            stored += 1
        connection.commit()
    except Exception:
        return stored
    finally:
        connection.close()
    return stored


def remember_search_resolution(
    server: Any,
    *,
    user_scope_id: str,
    query: str,
    entity_type: str,
    item: Dict[str, Any],
    confidence: float,
    event_weight: float = 1.0,
    event_type: str = "",
    source: str = "search_resolution",
) -> bool:
    if not isinstance(item, dict):
        return False
    item = annotate_source_identity(server, item)
    query_key = search_query_key(query)
    if not query_key:
        return False
    normalized_type = _text(entity_type) or "track"
    adapter = SearchServerAdapter(server)
    title_key, artist_key, album_key = canonical_track_fields(item, query=query)
    entity_key = _entity_key(normalized_type, item)
    if not entity_key:
        return False
    now = time.time()
    safe_confidence = max(0.0, min(float(confidence or 0.0), 1.0))
    weight = max(float(event_weight or 0.0), 0.0)
    normalized_event_type = _text(event_type) or _text(source) or "search_resolution"
    popularity_delta = _event_popularity_delta(normalized_event_type, weight)
    click_delta, play_delta, skip_delta = _event_counter_columns(normalized_event_type)
    entity_payload = dict(item)
    canonical_query_key = title_key or query_key
    source_identity = infer_source_identity(item)
    official_fields = _official_source_fields(item)
    memory_payload = {
        "source": source,
        "title": item.get("title") or item.get("name") or "",
        "artist": item.get("artist") or item.get("artist_name") or item.get("channel") or "",
        "thumbnail": item.get("thumbnail") or item.get("image") or "",
        "source_quality_score": item.get("source_quality_score"),
        "source_identity_authority": item.get("source_identity_authority") or "",
        "canonical_query_key": canonical_query_key,
    }
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return False
    try:
        connection.execute(
            """
            INSERT INTO search_canonical_entities(
                entity_type, entity_key, title_key, artist_key, album_key,
                source_authority, source_quality, popularity,
                click_count, play_count, skip_count, payload_json,
                official_source_provider, official_source_key,
                official_source_authority, official_confidence,
                learned_popularity, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_type, entity_key) DO UPDATE SET
                title_key = excluded.title_key,
                artist_key = excluded.artist_key,
                album_key = excluded.album_key,
                source_authority = excluded.source_authority,
                source_quality = max(search_canonical_entities.source_quality, excluded.source_quality),
                popularity = max(search_canonical_entities.popularity, excluded.popularity),
                click_count = search_canonical_entities.click_count + excluded.click_count,
                play_count = search_canonical_entities.play_count + excluded.play_count,
                skip_count = search_canonical_entities.skip_count + excluded.skip_count,
                payload_json = excluded.payload_json,
                official_source_provider = CASE
                    WHEN excluded.official_confidence >= search_canonical_entities.official_confidence
                    THEN excluded.official_source_provider
                    ELSE search_canonical_entities.official_source_provider
                END,
                official_source_key = CASE
                    WHEN excluded.official_confidence >= search_canonical_entities.official_confidence
                    THEN excluded.official_source_key
                    ELSE search_canonical_entities.official_source_key
                END,
                official_source_authority = CASE
                    WHEN excluded.official_confidence >= search_canonical_entities.official_confidence
                    THEN excluded.official_source_authority
                    ELSE search_canonical_entities.official_source_authority
                END,
                official_confidence = max(search_canonical_entities.official_confidence, excluded.official_confidence),
                learned_popularity = max(0, min(1.0, search_canonical_entities.learned_popularity + excluded.learned_popularity)),
                updated_at = excluded.updated_at
            """,
            [
                normalized_type,
                entity_key,
                title_key,
                artist_key,
                album_key,
                catalog_source_authority(item),
                source_quality_score(adapter, item),
                normalized_popularity(item),
                click_delta,
                play_delta,
                skip_delta,
                _json_dumps(entity_payload),
                official_fields["official_source_provider"],
                official_fields["official_source_key"],
                official_fields["official_source_authority"],
                official_fields["official_confidence"],
                popularity_delta,
                now,
            ],
        )
        connection.execute(
            """
            INSERT INTO search_entity_events(
                entity_type, entity_key, event_type, event_count, score, updated_at
            )
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(entity_type, entity_key, event_type) DO UPDATE SET
                event_count = search_entity_events.event_count + 1,
                score = search_entity_events.score + excluded.score,
                updated_at = excluded.updated_at
            """,
            [
                normalized_type,
                entity_key,
                normalized_event_type,
                popularity_delta,
                now,
            ],
        )
        for scope in _memory_scopes(user_scope_id):
            connection.execute(
                """
                INSERT INTO search_query_memory(
                    user_scope_id, query_key, entity_type, entity_key, title_key, artist_key,
                    score, confidence, event_count, payload_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(user_scope_id, query_key, entity_type, entity_key) DO UPDATE SET
                    score = search_query_memory.score + excluded.score,
                    confidence = max(search_query_memory.confidence, excluded.confidence),
                    event_count = search_query_memory.event_count + 1,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                [
                    scope,
                    query_key,
                    normalized_type,
                    entity_key,
                    title_key,
                    artist_key,
                    (safe_confidence * 1.5) + weight,
                    safe_confidence,
                    _json_dumps(memory_payload),
                    now,
                ],
            )
        for alias_key in _alias_keys_for_entity(
            query_key,
            title_key=title_key,
            artist_key=artist_key,
        ):
            connection.execute(
                """
                INSERT INTO search_query_aliases(
                    alias_key, canonical_query_key, entity_type, entity_key,
                    title_key, artist_key, score, confidence, event_count,
                    source, payload_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(alias_key, entity_type, entity_key) DO UPDATE SET
                    canonical_query_key = excluded.canonical_query_key,
                    title_key = excluded.title_key,
                    artist_key = excluded.artist_key,
                    score = search_query_aliases.score + excluded.score,
                    confidence = max(search_query_aliases.confidence, excluded.confidence),
                    event_count = search_query_aliases.event_count + 1,
                    source = excluded.source,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                [
                    alias_key,
                    canonical_query_key,
                    normalized_type,
                    entity_key,
                    title_key,
                    artist_key,
                    (safe_confidence * 1.8) + weight,
                    safe_confidence,
                    source,
                    _json_dumps(memory_payload),
                    now,
                ],
            )
        if source_identity:
            connection.execute(
                """
                INSERT INTO search_source_identities(
                    source_provider, source_key, source_name, authority,
                    confidence, evidence_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_provider, source_key) DO UPDATE SET
                    source_name = CASE
                        WHEN excluded.confidence >= search_source_identities.confidence
                        THEN excluded.source_name
                        ELSE search_source_identities.source_name
                    END,
                    authority = CASE
                        WHEN excluded.confidence >= search_source_identities.confidence
                        THEN excluded.authority
                        ELSE search_source_identities.authority
                    END,
                    confidence = max(search_source_identities.confidence, excluded.confidence),
                    evidence_json = excluded.evidence_json,
                    updated_at = excluded.updated_at
                """,
                [
                    _text(source_identity.get("source_provider")),
                    _text(source_identity.get("source_key")),
                    _text(source_identity.get("source_name")),
                    _text(source_identity.get("authority")),
                    float(source_identity.get("confidence") or 0.0),
                    _json_dumps(source_identity.get("evidence")),
                    now,
                ],
            )
        connection.commit()
        connection.close()
        remember_catalog_entity(
            server,
            user_scope_id=user_scope_id,
            query=query,
            entity_type=normalized_type,
            item=item,
            confidence=safe_confidence,
            event_weight=weight,
            event_type=normalized_event_type,
            source=source,
        )
        return True
    except Exception:
        return False
    finally:
        connection.close()
