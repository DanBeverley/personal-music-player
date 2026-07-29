from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import json
import threading
import time
from typing import Any, Dict, List, Tuple, TypedDict

from ..contracts import SearchRequest
from .server_adapter import DomainServerAdapter, adapt_domain_server
from ..recommend.profile_runtime import build_profile_key, hydrate_state_profile
from ..recommend.store_runtime import open_recommendation_store_connection


_NOISY_QUERY_PHRASES = (
    "deluxe remastered",
    "deluxe edition",
    "original soundtrack",
    "motion picture",
    "tribute to",
    "karaoke version",
    "radio edit",
    "from the motion picture",
)
_NOISY_QUERY_TOKENS = {
    "bonus",
    "deluxe",
    "edition",
    "karaoke",
    "mono",
    "original",
    "remaster",
    "remastered",
    "soundtrack",
    "stereo",
    "tribute",
    "version",
}

_HISTORY_SNAPSHOT_NAMESPACE = "history_profile_snapshot"
_HISTORY_SNAPSHOT_MODEL = "history-profile-snapshot"
_HISTORY_SNAPSHOT_CACHE: Dict[str, Dict[str, Any]] = {}
_HISTORY_SNAPSHOT_LOCK = threading.RLock()
_HISTORY_SNAPSHOT_BUILDS: Dict[str, threading.RLock] = {}
_HISTORY_SNAPSHOT_INFLIGHT: set[str] = set()
_HISTORY_SNAPSHOT_PENDING: set[str] = set()


class UserStateSnapshot(TypedDict, total=False):
    profile_key: str
    user_scope_id: str
    session_id: str
    surface: str
    recent_track_ids: List[str]
    top_track_ids: List[str]
    recent_track_snapshots: List[Dict[str, Any]]
    top_track_snapshots: List[Dict[str, Any]]
    anchor_track_snapshots: List[Dict[str, Any]]
    history_track_snapshots: List[Dict[str, Any]]
    frequent_track_snapshots: List[Dict[str, Any]]
    last_played_tracks: List[Dict[str, Any]]
    recent_queries: List[str]
    taste_queries: List[str]
    anchor_artist_hints: List[str]
    artist_hints: List[str]
    album_hints: List[str]
    playlist_names: List[str]
    library_track_ids: List[str]
    offline_track_ids: List[str]
    negative_track_ids: List[str]
    top_artists: List[str]
    listened_artists: List[str]
    top_albums: List[str]
    repeat_intensity: float
    novelty_tolerance: float
    novelty_preference: float
    repeat_tolerance: float
    experiment_variant: str
    vectors: Dict[str, Any]
    embedding_profile: Dict[str, Any]
    collaborative: Dict[str, Any]
    collaborative_profile: Dict[str, Any]
    profile_runtime: Dict[str, Any]


def trim_text(value: str | None) -> str:
    return adapt_domain_server().trim_text(value)


def unique_strings(values, limit: int | None = None) -> List[str]:
    return adapt_domain_server().unique_strings(values, limit)


def unique_snapshot_tracks(values, limit: int = 16) -> List[Dict[str, Any]]:
    return [
        dict(track)
        for track in adapt_domain_server().unique_snapshot_tracks(values or [], limit)
    ]


def build_search_request(**kwargs):
    return adapt_domain_server().build_search_request(**kwargs)


def _normalize_track_snapshots(raw_tracks, limit: int) -> List[Dict[str, Any]]:
    return unique_snapshot_tracks(raw_tracks or [], limit)


def _is_metadata_heavy_query(raw_query: str) -> bool:
    normalized = trim_text(raw_query).lower()
    if not normalized:
        return False
    if any(phrase in normalized for phrase in _NOISY_QUERY_PHRASES):
        return True
    hits = sum(1 for token in normalized.split() if token in _NOISY_QUERY_TOKENS)
    return hits >= 2


def _clean_query_values(values, *, limit: int) -> List[str]:
    cleaned: List[str] = []
    seen = set()
    for raw_value in values or []:
        value = trim_text(raw_value)
        if not value or _is_metadata_heavy_query(value):
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
        if len(cleaned) >= limit:
            break
    return cleaned


def _snapshot_artist_hints(server, tracks: List[Dict[str, Any]]) -> List[str]:
    hints: List[str] = []
    for track in tracks:
        hints.extend(server.extract_artist_names(track))
        channel_name = trim_text(track.get("channel"))
        if channel_name:
            hints.append(channel_name)
    return unique_strings(hints, 16)


def _sparse_home_request(
    *,
    recent_track_snapshots: List[Dict[str, Any]],
    top_track_snapshots: List[Dict[str, Any]],
    recent_queries: List[str],
    artist_hints: List[str],
    library_track_ids: List[str],
) -> bool:
    if len(recent_track_snapshots) >= 4 and len(top_track_snapshots) >= 4:
        return False
    signal_score = 0
    if recent_track_snapshots:
        signal_score += 2
    if len(recent_track_snapshots) >= 4:
        signal_score += 1
    if top_track_snapshots:
        signal_score += 2
    if len(top_track_snapshots) >= 4:
        signal_score += 1
    if len(artist_hints) >= 3:
        signal_score += 2
    elif artist_hints:
        signal_score += 1
    if len(recent_queries) >= 2:
        signal_score += 1
    if len(library_track_ids) >= 8:
        signal_score += 1
    return signal_score < 5


def _metadata_thumbnail_url(payload: Dict[str, Any]) -> str:
    thumbnail = payload.get("thumbnail")
    if isinstance(thumbnail, str) and thumbnail.strip():
        return thumbnail.strip()
    thumbnails = payload.get("thumbnails")
    if isinstance(thumbnails, list):
        for item in thumbnails:
            if isinstance(item, dict):
                url = trim_text(item.get("url"))
                if url:
                    return url
    return ""


def _metadata_duration(payload: Dict[str, Any]) -> int | None:
    raw_value = (
        payload.get("duration")
        or payload.get("duration_seconds")
        or payload.get("durationSeconds")
        or payload.get("lengthSeconds")
    )
    try:
        value = int(raw_value)
    except Exception:
        return None
    return value if value > 0 else None


def _snapshot_from_event_payload(
    *,
    track_id: str,
    artist_name: str,
    payload: Dict[str, Any],
) -> Dict[str, Any] | None:
    normalized_track_id = trim_text(track_id)
    if not normalized_track_id:
        return None
    playback = payload.get("playback") if isinstance(payload.get("playback"), dict) else {}
    playback_source_id = trim_text(
        playback.get("source_id")
        or payload.get("playback_source_id")
        or payload.get("videoId")
        or payload.get("video_id")
    )
    playback_provider = trim_text(
        playback.get("provider")
        or payload.get("source_provider")
        or payload.get("provider")
        or "youtube"
    ).lower()
    snapshot = {
        "id": normalized_track_id,
        "track_key": trim_text(payload.get("track_key")) or normalized_track_id,
        "title": trim_text(payload.get("title") or payload.get("name")),
        "channel": trim_text(
            payload.get("channel")
            or payload.get("artist")
            or payload.get("author")
            or artist_name
        ),
        "artist": trim_text(payload.get("artist") or payload.get("author") or artist_name),
        "album": trim_text(payload.get("album")),
        "album_id": trim_text(payload.get("album_id") or payload.get("albumId")),
        "thumbnail": _metadata_thumbnail_url(payload),
        "duration": _metadata_duration(payload),
    }
    if playback_source_id:
        snapshot["playback_source_id"] = playback_source_id
        snapshot["playback"] = {
            **playback,
            "provider": playback_provider,
            "source_id": playback_source_id,
        }
        if playback_provider == "youtube":
            snapshot["videoId"] = playback_source_id
    for key in (
        "canonical_entity_id",
        "canonical_track_identity",
        "musicbrainz_recording_id",
        "recording_mbid",
        "isrc",
        "release_year",
        "year",
    ):
        value = payload.get(key)
        if value not in (None, ""):
            snapshot[key] = value
    if not snapshot["title"] and not snapshot["channel"] and not snapshot["artist"]:
        return None
    return snapshot


def _build_scope_history_seed_from_events(
    server: DomainServerAdapter,
    user_scope_id: str,
) -> Dict[str, Any]:
    normalized_scope = trim_text(user_scope_id or "guest") or "guest"
    if normalized_scope == "guest":
        return {}
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return {}
    event_rows = []
    search_rows = []
    try:
        event_rows = connection.execute(
            """
            SELECT track_id, COALESCE(artist_name, '') AS artist_name,
                   COALESCE(event_type, 'play') AS event_type,
                   COALESCE(weight, 0) AS weight,
                   COALESCE(metadata_json, '{}') AS metadata_json,
                   occurred_at
            FROM recommendation_events
            WHERE user_scope_id = ?
              AND LOWER(COALESCE(event_type, 'play')) NOT IN ('impression', 'tab_tap')
            ORDER BY occurred_at DESC
            """,
            [normalized_scope],
        ).fetchall()
        search_rows = connection.execute(
            """
            SELECT query, COALESCE(result_count, 0) AS result_count, occurred_at
            FROM recommendation_search_events
            WHERE user_scope_id = ?
            ORDER BY occurred_at DESC
            LIMIT 40
            """,
            [normalized_scope],
        ).fetchall()
    except Exception:
        return {}
    finally:
        connection.close()

    diagnostics = {
        "persisted_event_count": len(list(event_rows or [])),
        "persisted_search_event_count": len(list(search_rows or [])),
        "persisted_scope": normalized_scope,
    }
    recent_track_ids: List[str] = []
    top_track_scores: Dict[str, float] = defaultdict(float)
    library_track_ids: List[str] = []
    artist_hints: List[str] = []
    track_payload_by_id: Dict[str, Dict[str, Any]] = {}
    history_snapshot_by_identity: Dict[str, Dict[str, Any]] = {}
    play_count_by_identity: Dict[str, int] = defaultdict(int)
    latest_play_by_identity: Dict[str, float] = {}
    last_counted_play_by_identity: Dict[str, float] = {}
    recent_track_snapshots: List[Dict[str, Any]] = []
    seen_recent_ids = set()
    seen_library_ids = set()
    negative_track_ids: List[str] = []
    seen_negative_ids = set()
    now = max(
        [float(row["occurred_at"] or 0.0) for row in list(event_rows or [])] or [0.0]
    )
    for index, row in enumerate(list(event_rows or [])):
        track_id = trim_text(row["track_id"])
        if not track_id:
            continue
        try:
            payload = json.loads(row["metadata_json"] or "{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        artist_name = trim_text(row["artist_name"])
        event_type = trim_text(row["event_type"]).lower() or "play"
        occurred_at = float(row["occurred_at"] or 0.0)
        age_days = max(0.0, (now - occurred_at) / 86400.0) if now > 0.0 else float(index) / 18.0
        recency_bonus = max(0.35, 1.45 - (age_days * 0.08))
        try:
            base_weight = float(row["weight"] or 0.0)
        except Exception:
            base_weight = 0.0
        is_negative_event = event_type in {"skip", "dislike", "hide", "remove", "remove_from_feed"}
        if is_negative_event or base_weight < 0.0:
            if track_id not in seen_negative_ids:
                seen_negative_ids.add(track_id)
                negative_track_ids.append(track_id)
            continue
        if event_type in {"impression", "tab_tap"}:
            continue
        if track_id in seen_negative_ids:
            continue
        if base_weight == 0.0:
            base_weight = 1.0
        top_track_scores[track_id] += base_weight * recency_bonus
        if track_id not in seen_recent_ids:
            seen_recent_ids.add(track_id)
            recent_track_ids.append(track_id)
        if artist_name:
            artist_hints.append(artist_name)
        snapshot = _snapshot_from_event_payload(
            track_id=track_id,
            artist_name=artist_name,
            payload=payload,
        )
        if snapshot is not None:
            canonical_identity = trim_text(
                snapshot.get("canonical_entity_id")
                or snapshot.get("canonical_track_identity")
                or snapshot.get("musicbrainz_recording_id")
                or snapshot.get("recording_mbid")
                or snapshot.get("isrc")
            )
            if not canonical_identity:
                title_key = " ".join(trim_text(snapshot.get("title")).casefold().split())
                artist_key = " ".join(
                    trim_text(snapshot.get("artist") or snapshot.get("channel")).casefold().split()
                )
                canonical_identity = f"{title_key}|{artist_key}" if title_key and artist_key else track_id
            snapshot["canonical_entity_id"] = canonical_identity
            if canonical_identity not in history_snapshot_by_identity:
                history_snapshot_by_identity[canonical_identity] = dict(snapshot)
            if event_type == "play":
                last_counted = last_counted_play_by_identity.get(canonical_identity)
                if last_counted is None or last_counted - occurred_at >= 30.0:
                    play_count_by_identity[canonical_identity] += 1
                    last_counted_play_by_identity[canonical_identity] = occurred_at
                latest_play_by_identity[canonical_identity] = max(
                    latest_play_by_identity.get(canonical_identity, 0.0),
                    occurred_at,
                )
            track_payload_by_id[track_id] = snapshot
            recent_track_snapshots.append(snapshot)
            if snapshot.get("album"):
                artist_hints.extend(server.extract_artist_names(snapshot))
        if event_type in {"library", "download", "save"} and track_id not in seen_library_ids:
            seen_library_ids.add(track_id)
            library_track_ids.append(track_id)

    recent_track_ids = server.unique_track_ids(recent_track_ids, 16)
    top_track_ids = [
        track_id
        for track_id, _score in sorted(
            top_track_scores.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:16]
    ]

    fetch_ids = [
        track_id
        for track_id in server.unique_track_ids(
            [*recent_track_ids, *top_track_ids],
            24,
        )
        if track_id not in track_payload_by_id
    ]
    if fetch_ids:
        try:
            fetched_tracks = server.recommendation_fetch_tracks_for_ids(fetch_ids, limit=len(fetch_ids))
        except Exception:
            fetched_tracks = []
        for track in list(fetched_tracks or []):
            if not isinstance(track, dict):
                continue
            track_id = trim_text(track.get("id"))
            if not track_id or track_id in track_payload_by_id:
                continue
            normalized = server.normalize_recommendation_track(track)
            if normalized is None:
                continue
            track_payload_by_id[track_id] = server.merge_track_metadata(track, normalized)

    recent_track_snapshots = unique_snapshot_tracks(
        [
            track_payload_by_id.get(track_id) or {}
            for track_id in recent_track_ids
            if track_payload_by_id.get(track_id)
        ],
        16,
    )
    top_track_snapshots = unique_snapshot_tracks(
        [
            track_payload_by_id.get(track_id) or {}
            for track_id in top_track_ids
            if track_payload_by_id.get(track_id)
        ],
        16,
    )
    last_played_tracks = unique_snapshot_tracks(recent_track_snapshots[:12], 12)
    history_track_snapshots: List[Dict[str, Any]] = []
    for identity, snapshot in sorted(
        history_snapshot_by_identity.items(),
        key=lambda item: latest_play_by_identity.get(item[0], 0.0),
        reverse=True,
    ):
        enriched = dict(snapshot)
        enriched["canonical_entity_id"] = identity
        enriched["play_count"] = int(play_count_by_identity.get(identity, 0))
        enriched["last_played_at"] = float(latest_play_by_identity.get(identity, 0.0))
        history_track_snapshots.append(enriched)
    frequent_track_snapshots = sorted(
        [track for track in history_track_snapshots if int(track.get("play_count") or 0) > 0],
        key=lambda track: (
            int(track.get("play_count") or 0),
            float(track.get("last_played_at") or 0.0),
        ),
        reverse=True,
    )
    anchor_track_snapshots = unique_snapshot_tracks(
        [*last_played_tracks[:4], *top_track_snapshots[:4]],
        8,
    )
    recent_queries = _clean_query_values(
        [row["query"] for row in list(search_rows or []) if int(row["result_count"] or 0) >= 0],
        limit=12,
    )
    taste_queries = _clean_query_values(
        [row["query"] for row in list(search_rows or []) if int(row["result_count"] or 0) > 0],
        limit=8,
    )
    return {
        "recent_track_ids": recent_track_ids,
        "top_track_ids": top_track_ids,
        "recent_track_snapshots": recent_track_snapshots,
        "top_track_snapshots": top_track_snapshots,
        "last_played_tracks": last_played_tracks,
        "anchor_track_snapshots": anchor_track_snapshots,
        "history_track_snapshots": history_track_snapshots,
        "frequent_track_snapshots": frequent_track_snapshots,
        "recent_queries": recent_queries,
        "taste_queries": taste_queries,
        "artist_hints": unique_strings(artist_hints, 12),
        "library_track_ids": server.unique_track_ids(library_track_ids, 28),
        "negative_track_ids": server.unique_track_ids(negative_track_ids, 28),
        "_diagnostics": diagnostics,
    }


def _history_snapshot_copy(seed: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    output = deepcopy(seed)
    diagnostics = dict(output.get("_diagnostics") or {})
    diagnostics["history_snapshot_source"] = source
    output["_diagnostics"] = diagnostics
    return output


def _load_persisted_history_snapshot(
    server: DomainServerAdapter,
    user_scope_id: str,
) -> Dict[str, Any] | None:
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return None
    try:
        row = connection.execute(
            """
            SELECT payload_json
            FROM recommendation_feature_store
            WHERE namespace = ? AND entity_id = ?
            """,
            [_HISTORY_SNAPSHOT_NAMESPACE, user_scope_id],
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"] or "{}")
        if (
            not isinstance(payload, dict)
            or payload.get("snapshot_model") != _HISTORY_SNAPSHOT_MODEL
            or not isinstance(payload.get("seed"), dict)
        ):
            return None
        return dict(payload["seed"])
    except Exception:
        return None
    finally:
        connection.close()


def _store_history_snapshot(
    server: DomainServerAdapter,
    user_scope_id: str,
    seed: Dict[str, Any],
) -> bool:
    payload = {
        "snapshot_model": _HISTORY_SNAPSHOT_MODEL,
        "user_scope_id": user_scope_id,
        "seed": seed,
        "updated_at": time.time(),
    }
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return False
    try:
        connection.execute(
            """
            INSERT INTO recommendation_feature_store(
                namespace, entity_id, model_id, payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(namespace, entity_id) DO UPDATE SET
                model_id = excluded.model_id,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            [
                _HISTORY_SNAPSHOT_NAMESPACE,
                user_scope_id,
                _HISTORY_SNAPSHOT_MODEL,
                json.dumps(payload, ensure_ascii=False),
                payload["updated_at"],
            ],
        )
        connection.commit()
    except Exception:
        return False
    finally:
        connection.close()
    with _HISTORY_SNAPSHOT_LOCK:
        _HISTORY_SNAPSHOT_CACHE[user_scope_id] = deepcopy(seed)
    return True


def _history_build_lock(user_scope_id: str) -> threading.RLock:
    with _HISTORY_SNAPSHOT_LOCK:
        return _HISTORY_SNAPSHOT_BUILDS.setdefault(user_scope_id, threading.RLock())


def _load_scope_history_seed(
    server: DomainServerAdapter,
    user_scope_id: str,
) -> Dict[str, Any]:
    normalized_scope = trim_text(user_scope_id or "guest") or "guest"
    if normalized_scope == "guest":
        return {}
    with _HISTORY_SNAPSHOT_LOCK:
        cached = _HISTORY_SNAPSHOT_CACHE.get(normalized_scope)
    if isinstance(cached, dict):
        return _history_snapshot_copy(cached, source="memory")

    with _history_build_lock(normalized_scope):
        with _HISTORY_SNAPSHOT_LOCK:
            cached = _HISTORY_SNAPSHOT_CACHE.get(normalized_scope)
        if isinstance(cached, dict):
            return _history_snapshot_copy(cached, source="memory")
        persisted = _load_persisted_history_snapshot(server, normalized_scope)
        if isinstance(persisted, dict):
            with _HISTORY_SNAPSHOT_LOCK:
                _HISTORY_SNAPSHOT_CACHE[normalized_scope] = deepcopy(persisted)
            return _history_snapshot_copy(persisted, source="persistent")
        seed = _build_scope_history_seed_from_events(server, normalized_scope)
        if seed:
            _store_history_snapshot(server, normalized_scope, seed)
        return _history_snapshot_copy(seed, source="rebuilt") if seed else {}


def schedule_history_seed_snapshot_refresh(
    server: Any,
    user_scope_id: str,
) -> bool:
    domain_server = adapt_domain_server(server)
    normalized_scope = trim_text(user_scope_id or "guest") or "guest"
    if normalized_scope == "guest":
        return False
    with _HISTORY_SNAPSHOT_LOCK:
        if normalized_scope in _HISTORY_SNAPSHOT_INFLIGHT:
            _HISTORY_SNAPSHOT_PENDING.add(normalized_scope)
            return False
        _HISTORY_SNAPSHOT_INFLIGHT.add(normalized_scope)

    def refresh() -> None:
        try:
            seed = _build_scope_history_seed_from_events(
                domain_server,
                normalized_scope,
            )
            if seed:
                _store_history_snapshot(domain_server, normalized_scope, seed)
        finally:
            rerun = False
            with _HISTORY_SNAPSHOT_LOCK:
                _HISTORY_SNAPSHOT_INFLIGHT.discard(normalized_scope)
                if normalized_scope in _HISTORY_SNAPSHOT_PENDING:
                    _HISTORY_SNAPSHOT_PENDING.discard(normalized_scope)
                    rerun = True
            if rerun:
                schedule_history_seed_snapshot_refresh(
                    domain_server.raw,
                    normalized_scope,
                )

    executor = getattr(domain_server.raw, "precompute_executor", None)
    if executor is None:
        with _HISTORY_SNAPSHOT_LOCK:
            _HISTORY_SNAPSHOT_INFLIGHT.discard(normalized_scope)
        return False
    try:
        executor.submit(refresh)
        return True
    except Exception:
        with _HISTORY_SNAPSHOT_LOCK:
            _HISTORY_SNAPSHOT_INFLIGHT.discard(normalized_scope)
        return False


def _build_state_snapshot(legacy_req, *, server: Any | None = None) -> UserStateSnapshot:
    server = adapt_domain_server(server)
    recent_track_snapshots = server.unique_snapshot_tracks(
        [*(legacy_req.last_played_tracks or []), *(legacy_req.recent_track_snapshots or [])],
        16,
    )
    top_track_snapshots = server.unique_snapshot_tracks(
        legacy_req.top_track_snapshots,
        16,
    )
    last_played_tracks = server.unique_snapshot_tracks(
        legacy_req.last_played_tracks,
        12,
    )
    anchor_track_snapshots = server.unique_snapshot_tracks(
        legacy_req.anchor_track_snapshots,
        8,
    )

    recent_track_ids = server.unique_track_ids(
        [
            legacy_req.seed_id,
            *(track.get("id") for track in recent_track_snapshots),
            *(legacy_req.seed_ids or []),
            *(legacy_req.recent_track_ids or []),
        ],
        16,
    )
    top_track_ids = server.unique_track_ids(
        [
            *(track.get("id") for track in top_track_snapshots),
            *(legacy_req.top_track_ids or []),
            *(legacy_req.seed_ids or []),
            legacy_req.seed_id,
        ],
        16,
    )
    taste_queries = _clean_query_values(legacy_req.taste_queries, limit=12)
    recent_queries = _clean_query_values(
        [*(legacy_req.recent_queries or []), legacy_req.query, *taste_queries],
        limit=12,
    )
    explicit_artist_hints = unique_strings(legacy_req.artist_hints, 12)
    library_track_ids = server.unique_track_ids(
        legacy_req.library_track_ids,
        28,
    )
    negative_track_ids: List[str] = []
    client_signal_diagnostics = {
        "client_recent_track_count": len(recent_track_snapshots),
        "client_top_track_count": len(top_track_snapshots),
        "client_last_played_count": len(last_played_tracks),
        "client_recent_seed_count": len(recent_track_ids),
        "client_top_seed_count": len(top_track_ids),
        "client_recent_query_count": len(recent_queries),
        "client_artist_hint_count": len(explicit_artist_hints),
        "client_library_track_count": len(library_track_ids),
    }
    persisted_diagnostics: Dict[str, Any] = {}
    sparse_request = _sparse_home_request(
        recent_track_snapshots=recent_track_snapshots,
        top_track_snapshots=top_track_snapshots,
        recent_queries=recent_queries,
        artist_hints=explicit_artist_hints,
        library_track_ids=library_track_ids,
    )
    persisted = _load_scope_history_seed(
        server,
        trim_text(legacy_req.user_scope_id or "guest") or "guest",
    )
    persisted_diagnostics = dict(persisted.get("_diagnostics") or {}) if persisted else {}
    history_track_snapshots: List[Dict[str, Any]] = []
    frequent_track_snapshots: List[Dict[str, Any]] = []
    if persisted:
        recent_track_snapshots = server.unique_snapshot_tracks(
            [
                *recent_track_snapshots,
                *(persisted.get("recent_track_snapshots") or []),
            ],
            16,
        )
        top_track_snapshots = server.unique_snapshot_tracks(
            [
                *top_track_snapshots,
                *(persisted.get("top_track_snapshots") or []),
            ],
            16,
        )
        last_played_tracks = server.unique_snapshot_tracks(
            [
                *last_played_tracks,
                *(persisted.get("last_played_tracks") or []),
            ],
            12,
        )
        anchor_track_snapshots = server.unique_snapshot_tracks(
            [
                *anchor_track_snapshots,
                *(persisted.get("anchor_track_snapshots") or []),
            ],
            8,
        )
        history_track_snapshots = [
            dict(track)
            for track in persisted.get("history_track_snapshots") or []
            if isinstance(track, dict)
        ]
        frequent_track_snapshots = [
            dict(track)
            for track in persisted.get("frequent_track_snapshots") or []
            if isinstance(track, dict)
        ]
        recent_track_ids = server.unique_track_ids(
            [
                *(recent_track_ids or []),
                *(persisted.get("recent_track_ids") or []),
            ],
            16,
        )
        top_track_ids = server.unique_track_ids(
            [
                *(top_track_ids or []),
                *(persisted.get("top_track_ids") or []),
            ],
            16,
        )
        recent_queries = _clean_query_values(
            [
                *recent_queries,
                *(persisted.get("recent_queries") or []),
            ],
            limit=12,
        )
        taste_queries = _clean_query_values(
            [
                *taste_queries,
                *(persisted.get("taste_queries") or []),
            ],
            limit=12,
        )
        explicit_artist_hints = unique_strings(
            [
                *explicit_artist_hints,
                *(persisted.get("artist_hints") or []),
            ],
            12,
        )
        library_track_ids = server.unique_track_ids(
            [
                *library_track_ids,
                *(persisted.get("library_track_ids") or []),
            ],
            28,
        )
        negative_track_ids = server.unique_track_ids(
            persisted.get("negative_track_ids") or [],
            28,
        )
    if negative_track_ids:
        negative_ids = set(negative_track_ids)
        recent_track_ids = [track_id for track_id in recent_track_ids if track_id not in negative_ids]
        top_track_ids = [track_id for track_id in top_track_ids if track_id not in negative_ids]
        recent_track_snapshots = [
            track
            for track in recent_track_snapshots
            if trim_text(track.get("id")) not in negative_ids
        ]
        top_track_snapshots = [
            track
            for track in top_track_snapshots
            if trim_text(track.get("id")) not in negative_ids
        ]
        last_played_tracks = [
            track
            for track in last_played_tracks
            if trim_text(track.get("id")) not in negative_ids
        ]
        anchor_track_snapshots = [
            track
            for track in anchor_track_snapshots
            if trim_text(track.get("id")) not in negative_ids
        ]
        history_track_snapshots = [
            track
            for track in history_track_snapshots
            if trim_text(track.get("id")) not in negative_ids
        ]
        frequent_track_snapshots = [
            track
            for track in frequent_track_snapshots
            if trim_text(track.get("id")) not in negative_ids
        ]
    anchor_artist_hints = unique_strings(
        [
            *(legacy_req.anchor_artist_hints or []),
            *_snapshot_artist_hints(server, anchor_track_snapshots),
        ],
        8,
    )
    combined_tracks = [
        *anchor_track_snapshots,
        *top_track_snapshots,
        *recent_track_snapshots,
        *last_played_tracks,
    ]
    artist_hints = unique_strings(
        [
            *anchor_artist_hints,
            *explicit_artist_hints,
            *_snapshot_artist_hints(server, combined_tracks),
        ],
        12,
    )
    album_hints = unique_strings(
        [
            *(legacy_req.album_hints or []),
            *[
                trim_text(track.get("album"))
                for track in combined_tracks
                if trim_text(track.get("album"))
            ],
        ],
        12,
    )
    playlist_names = unique_strings(legacy_req.playlist_names, 12)
    offline_track_ids = server.unique_track_ids(
        legacy_req.offline_track_ids,
        28,
    )

    artist_weights: Dict[str, float] = {}
    for index, track in enumerate(top_track_snapshots):
        for artist_name in server.extract_artist_names(track):
            artist_weights[artist_name] = artist_weights.get(artist_name, 0.0) + max(
                1.8 - (index * 0.14),
                0.55,
            )
    for index, track in enumerate(recent_track_snapshots):
        for artist_name in server.extract_artist_names(track):
            artist_weights[artist_name] = artist_weights.get(artist_name, 0.0) + max(
                1.25 - (index * 0.1),
                0.35,
            )
    ranked_artist_names = [
        item[0]
        for item in sorted(
            artist_weights.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ]
    top_artist_names = unique_strings([*ranked_artist_names, *artist_hints], 6)
    listened_artist_names = unique_strings([*ranked_artist_names, *artist_hints], 12)
    top_album_names = album_hints[:6]
    repeat_intensity = min(1.0, len(top_track_ids) / 10.0)
    novelty_tolerance = max(
        0.25,
        min(0.75, 0.38 + (len(recent_queries) * 0.04) + (len(artist_hints) * 0.02)),
    )

    profile: UserStateSnapshot = {
        "profile_key": build_profile_key(server, legacy_req),
        "user_scope_id": trim_text(legacy_req.user_scope_id or "guest") or "guest",
        "session_id": trim_text(legacy_req.session_id or ""),
        "surface": trim_text(legacy_req.surface or "home_feed") or "home_feed",
        "recent_track_ids": recent_track_ids,
        "top_track_ids": top_track_ids,
        "recent_track_snapshots": recent_track_snapshots,
        "top_track_snapshots": top_track_snapshots,
        "anchor_track_snapshots": anchor_track_snapshots,
        "history_track_snapshots": history_track_snapshots,
        "frequent_track_snapshots": frequent_track_snapshots,
        "last_played_tracks": last_played_tracks,
        "recent_queries": recent_queries,
        "taste_queries": taste_queries,
        "anchor_artist_hints": anchor_artist_hints,
        "artist_hints": artist_hints,
        "album_hints": album_hints,
        "playlist_names": playlist_names,
        "library_track_ids": library_track_ids,
        "offline_track_ids": offline_track_ids,
        "negative_track_ids": negative_track_ids,
        "top_artists": top_artist_names,
        "listened_artists": listened_artist_names,
        "top_albums": top_album_names,
        "repeat_intensity": repeat_intensity,
        "novelty_tolerance": novelty_tolerance,
        "novelty_preference": novelty_tolerance,
        "repeat_tolerance": max(0.0, 1.0 - repeat_intensity),
        "experiment_variant": server.recommendation_assignment_for_user(
            trim_text(legacy_req.user_scope_id or "guest") or "guest"
        ),
        "signal_diagnostics": {
            "sparse_request": bool(sparse_request),
            **client_signal_diagnostics,
            "resolved_recent_track_count": len(recent_track_snapshots),
            "resolved_top_track_count": len(top_track_snapshots),
            "resolved_last_played_count": len(last_played_tracks),
            "resolved_recent_seed_count": len(recent_track_ids),
            "resolved_top_seed_count": len(top_track_ids),
            "resolved_recent_query_count": len(recent_queries),
            "resolved_artist_hint_count": len(explicit_artist_hints),
            "resolved_library_track_count": len(library_track_ids),
            **persisted_diagnostics,
        },
    }
    return hydrate_state_profile(
        server,
        profile,
        force_refresh=bool(getattr(legacy_req, "force_refresh", False)),
    )


def build_home_state(
    req: SearchRequest,
    *,
    server: Any | None = None,
) -> Tuple[Any, UserStateSnapshot]:
    raw_recent_tracks = (
        getattr(req, "recent_tracks", None)
        or getattr(req, "recent_track_snapshots", None)
        or []
    )
    raw_top_tracks = (
        getattr(req, "top_tracks", None)
        or getattr(req, "top_track_snapshots", None)
        or []
    )
    raw_last_played_tracks = getattr(req, "last_played_tracks", None) or raw_recent_tracks
    recent_tracks = _normalize_track_snapshots(raw_recent_tracks, 16)
    top_tracks = _normalize_track_snapshots(raw_top_tracks, 16)
    last_played_tracks = _normalize_track_snapshots(raw_last_played_tracks, 12)
    recent_queries = _clean_query_values(
        [*req.recent_queries, *req.taste_queries, req.query],
        limit=12,
    )
    taste_queries = _clean_query_values(
        [*req.taste_queries, *recent_queries],
        limit=8,
    )
    legacy_req = build_search_request(
        query="",
        limit=req.limit,
        user_scope_id=req.user_scope_id or "guest",
        session_id=req.session_id,
        surface="home_feed",
        force_refresh=req.force_refresh,
        recent_queries=recent_queries,
        taste_queries=taste_queries,
        artist_hints=list(req.artist_hints or []),
        album_hints=list(req.album_hints or []),
        avoid_ids=list(req.avoid_ids or []),
        recent_track_ids=list(req.recent_track_ids or []),
        top_track_ids=list(req.top_track_ids or []),
        recent_track_snapshots=recent_tracks,
        top_track_snapshots=top_tracks or recent_tracks[:8],
        last_played_tracks=last_played_tracks or recent_tracks[:8],
        playlist_names=list(req.playlist_names or []),
        library_track_ids=list(req.library_track_ids or []),
        offline_track_ids=list(req.offline_track_ids or []),
    )
    return legacy_req, _build_state_snapshot(legacy_req, server=server)


def build_similar_artists_state(
    req: SearchRequest,
    *,
    server: Any | None = None,
) -> Tuple[Any, UserStateSnapshot]:
    server = adapt_domain_server(server)
    recent_tracks = _normalize_track_snapshots(req.recent_tracks, 12)
    anchor_track_snapshots = _normalize_track_snapshots(
        [req.anchor_track_snapshot] if req.anchor_track_snapshot else [],
        4,
    )
    if not anchor_track_snapshots and req.anchor_track_id:
        fetched = server.recommendation_fetch_tracks_for_ids([req.anchor_track_id], limit=1)
        anchor_track_snapshots = _normalize_track_snapshots(fetched, 1)
    anchor_artist_hints: List[str] = []
    if req.anchor_artist_id:
        try:
            artist_payload = server.build_artist_details_payload(req.anchor_artist_id)
        except Exception:
            artist_payload = {}
        artist_name = trim_text((artist_payload.get("artist") or {}).get("name"))
        if artist_name:
            anchor_artist_hints.append(artist_name)
    cleaned_queries = _clean_query_values(req.recent_queries, limit=12)
    legacy_req = build_search_request(
        query=req.query or "",
        limit=req.limit,
        user_scope_id=req.user_scope_id or "guest",
        session_id=req.session_id,
        surface=req.surface or "search_results",
        recent_queries=cleaned_queries,
        taste_queries=cleaned_queries[:8],
        recent_track_snapshots=recent_tracks,
        top_track_snapshots=recent_tracks[:8],
        last_played_tracks=recent_tracks[:8],
        anchor_track_snapshots=anchor_track_snapshots,
        anchor_artist_hints=anchor_artist_hints,
    )
    return legacy_req, _build_state_snapshot(legacy_req, server=server)
