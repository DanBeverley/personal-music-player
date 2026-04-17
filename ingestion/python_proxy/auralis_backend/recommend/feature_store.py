from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import re
from threading import Lock
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..domain.catalog import (
    canonical_album_identity,
    canonical_artist_identity,
    canonical_title_artist_identity,
)
from ..storage.postgres import db_available, get_connection
from .store_runtime import open_recommendation_store_connection


CATALOG_FEATURE_VERSION = "catalog-feature-v1"
TASTE_PROFILE_VERSION = "taste-profile-v1"
SCENE_GRAPH_VERSION = "scene-graph-v1"
_HOT_CACHE_TTL_SECONDS = max(
    30,
    int(os.environ.get("AURALIS_FEATURE_HOT_CACHE_TTL_SECONDS", "600")),
)


_SCHEMA_LOCK = Lock()
_SQLITE_SCHEMA_READY = False
_POSTGRES_SCHEMA_READY = False
_HOT_CACHE_LOCK = Lock()
_REQUEST_STORE_RUNTIME: ContextVar[Dict[str, Any] | None] = ContextVar(
    "auralis_request_store_runtime",
    default=None,
)
_CACHE_MISS = object()
_HOT_CACHES: Dict[str, Dict[str, Tuple[float, Any]]] = {
    "catalog": {},
    "taste": {},
    "negative": {},
    "scene_graph": {},
}


_SQLITE_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS recommendation_catalog_features (
        entity_type TEXT NOT NULL,
        entity_key TEXT NOT NULL,
        external_id TEXT,
        feature_version TEXT NOT NULL,
        scene_graph_version TEXT NOT NULL DEFAULT '',
        source_kind TEXT NOT NULL DEFAULT '',
        confidence REAL NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}',
        updated_at REAL NOT NULL,
        PRIMARY KEY (entity_type, entity_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_recommendation_catalog_features_external
    ON recommendation_catalog_features(entity_type, external_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS recommendation_taste_profiles (
        user_scope_id TEXT PRIMARY KEY,
        profile_version TEXT NOT NULL,
        source_signature TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recommendation_negative_feedback (
        user_scope_id TEXT NOT NULL,
        feedback_type TEXT NOT NULL,
        feedback_key TEXT NOT NULL,
        strength REAL NOT NULL DEFAULT 0,
        expires_at REAL NOT NULL DEFAULT 0,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        updated_at REAL NOT NULL,
        PRIMARY KEY (user_scope_id, feedback_type, feedback_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_recommendation_negative_feedback_user_exp
    ON recommendation_negative_feedback(user_scope_id, expires_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS recommendation_scene_graph_records (
        graph_kind TEXT NOT NULL,
        graph_key TEXT NOT NULL,
        graph_version TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        updated_at REAL NOT NULL,
        PRIMARY KEY (graph_kind, graph_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_recommendation_scene_graph_kind
    ON recommendation_scene_graph_records(graph_kind, updated_at DESC)
    """,
)

_POSTGRES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS recommendation_catalog_features (
  entity_type text NOT NULL,
  entity_key text NOT NULL,
  external_id text DEFAULT '',
  feature_version text NOT NULL,
  scene_graph_version text NOT NULL DEFAULT '',
  source_kind text NOT NULL DEFAULT '',
  confidence double precision NOT NULL DEFAULT 0,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (entity_type, entity_key)
);
CREATE INDEX IF NOT EXISTS idx_recommendation_catalog_features_external
ON recommendation_catalog_features(entity_type, external_id);
CREATE TABLE IF NOT EXISTS recommendation_taste_profiles (
  user_scope_id text PRIMARY KEY,
  profile_version text NOT NULL,
  source_signature text NOT NULL DEFAULT '',
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS recommendation_negative_feedback (
  user_scope_id text NOT NULL,
  feedback_type text NOT NULL,
  feedback_key text NOT NULL,
  strength double precision NOT NULL DEFAULT 0,
  expires_at double precision NOT NULL DEFAULT 0,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_scope_id, feedback_type, feedback_key)
);
CREATE INDEX IF NOT EXISTS idx_recommendation_negative_feedback_user_exp
ON recommendation_negative_feedback(user_scope_id, expires_at DESC);
CREATE TABLE IF NOT EXISTS recommendation_scene_graph_records (
  graph_kind text NOT NULL,
  graph_key text NOT NULL,
  graph_version text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (graph_kind, graph_key)
);
CREATE INDEX IF NOT EXISTS idx_recommendation_scene_graph_kind
ON recommendation_scene_graph_records(graph_kind, updated_at DESC);
"""

_SCRIPT_PATTERNS = {
    "devanagari": re.compile(r"[\u0900-\u097F]"),
    "arabic": re.compile(r"[\u0600-\u06FF]"),
    "cyrillic": re.compile(r"[\u0400-\u04FF]"),
    "han": re.compile(r"[\u4E00-\u9FFF]"),
    "kana": re.compile(r"[\u3040-\u30FF]"),
    "hangul": re.compile(r"[\uAC00-\uD7AF]"),
}

_TYPE_PATTERNS = {
    "acoustic": re.compile(r"\b(acoustic|stripped|unplugged)\b", re.IGNORECASE),
    "live": re.compile(r"\b(live|live at|live from|session live)\b", re.IGNORECASE),
    "instrumental": re.compile(
        r"\b(instrumental|orchestral|piano version|strings version|score)\b",
        re.IGNORECASE,
    ),
    "remix": re.compile(r"\b(remix|rework|club mix|dance mix|edit)\b", re.IGNORECASE),
    "remaster": re.compile(r"\b(remaster|remastered|20\d\d remaster|201\d remaster)\b", re.IGNORECASE),
    "cover": re.compile(r"\b(cover|tribute|karaoke)\b", re.IGNORECASE),
}

_GENRE_RULES: Sequence[Tuple[str, str, re.Pattern[str]]] = (
    ("rock", "hard_rock", re.compile(r"\b(guns n roses|guns n' roses|acdc|ac/dc|aerosmith|hard rock|arena rock|appetite for destruction)\b", re.IGNORECASE)),
    ("rock", "classic_rock", re.compile(r"\b(classic rock|led zeppelin|eagles|queen|bon jovi|scorpions|poison|journey|fleetwood mac|foreigner)\b", re.IGNORECASE)),
    ("rock", "alternative_rock", re.compile(r"\b(alternative rock|alt rock|radiohead|coldplay|the killers|foo fighters)\b", re.IGNORECASE)),
    ("rock", "grunge", re.compile(r"\b(grunge|nirvana|soundgarden|pearl jam|alice in chains)\b", re.IGNORECASE)),
    ("metal", "heavy_metal", re.compile(r"\b(metal|heavy metal|black sabbath|iron maiden|metallica|megadeth|slayer)\b", re.IGNORECASE)),
    ("pop", "synth_pop", re.compile(r"\b(pop|synth pop|synth-pop|taylor swift|katy perry|dua lipa|madonna)\b", re.IGNORECASE)),
    ("hip_hop", "rap", re.compile(r"\b(hip hop|hip-hop|rap|kanye west|kendrick lamar|drake|eminem|jay z|jay-z)\b", re.IGNORECASE)),
    ("rnb", "soul", re.compile(r"\b(rnb|r&b|soul|neo soul|neo-soul|marvin gaye|sza|the weeknd)\b", re.IGNORECASE)),
    ("electronic", "edm", re.compile(r"\b(edm|electronic|house|techno|trance|club mix|dance mix|daft punk|calvin harris)\b", re.IGNORECASE)),
    ("electronic", "ambient", re.compile(r"\b(ambient|sleep|focus|chillout|downtempo|meditation)\b", re.IGNORECASE)),
    ("country", "country", re.compile(r"\b(country|nashville|americana|morgan wallen|johnny cash)\b", re.IGNORECASE)),
    ("jazz", "jazz", re.compile(r"\b(jazz|bebop|swing|miles davis|coltrane|ella fitzgerald)\b", re.IGNORECASE)),
    ("blues", "blues", re.compile(r"\b(blues|bb king|b\\.b\\. king|muddy waters|eric clapton)\b", re.IGNORECASE)),
    ("folk", "folk", re.compile(r"\b(folk|singer songwriter|singer-songwriter|bob dylan|joan baez|acoustic)\b", re.IGNORECASE)),
    ("latin", "latin_pop", re.compile(r"\b(latin|reggaeton|bachata|salsa|corridos|spanish|en espa[ñn]ol|gipsy kings)\b", re.IGNORECASE)),
    ("devotional", "devotional", re.compile(r"\b(bhajan|devotional|qawwali|worship|gospel|nasheed)\b", re.IGNORECASE)),
    ("soundtrack", "soundtrack", re.compile(r"\b(soundtrack|original score|motion picture|ost)\b", re.IGNORECASE)),
)

_LANGUAGE_HINTS: Sequence[Tuple[str, str, re.Pattern[str]]] = (
    ("spanish", "latin_america", re.compile(r"\b(hola|amor|corazon|corazón|vida|canci[oó]n|en espa[ñn]ol|latino|gipsy kings)\b", re.IGNORECASE)),
    ("portuguese", "brazil", re.compile(r"\b(saudade|brasil|sertanejo|mpb)\b", re.IGNORECASE)),
    ("french", "francophone", re.compile(r"\b(amour|bonjour|chanson|paris)\b", re.IGNORECASE)),
    ("hindi", "india", re.compile(r"[\u0900-\u097F]")),
    ("arabic", "mena", re.compile(r"[\u0600-\u06FF]")),
    ("cyrillic", "cis", re.compile(r"[\u0400-\u04FF]")),
    ("cjk", "east_asia", re.compile(r"[\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]")),
)


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _json_loads(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _normalize_sequence(values: Sequence[str] | None) -> List[str]:
    output: List[str] = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _copy_cached_value(value: Any) -> Any:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return list(value)
    return value


@contextmanager
def request_store_runtime(*, allow_persistent_reads: bool = False):
    existing = _REQUEST_STORE_RUNTIME.get()
    if isinstance(existing, dict):
        yield existing
        return
    runtime = {
        "allow_persistent_reads": bool(allow_persistent_reads),
        "caches": {
            "catalog": {},
            "taste": {},
            "negative": {},
            "scene_graph": {},
        },
    }
    token = _REQUEST_STORE_RUNTIME.set(runtime)
    try:
        yield runtime
    finally:
        _REQUEST_STORE_RUNTIME.reset(token)


def persistent_store_reads_enabled() -> bool:
    runtime = _REQUEST_STORE_RUNTIME.get()
    if not isinstance(runtime, dict):
        return True
    return bool(runtime.get("allow_persistent_reads"))


def request_runtime_cache_get(bucket: str, key: str) -> Tuple[bool, Any]:
    runtime = _REQUEST_STORE_RUNTIME.get()
    if not isinstance(runtime, dict):
        return False, None
    caches = runtime.setdefault("caches", {})
    cache = caches.setdefault(bucket, {})
    if key not in cache:
        return False, None
    value = cache.get(key)
    if value is _CACHE_MISS:
        return True, None
    return True, _copy_cached_value(value)


def request_runtime_cache_put(bucket: str, key: str, value: Any) -> Any:
    runtime = _REQUEST_STORE_RUNTIME.get()
    if not isinstance(runtime, dict):
        return value
    caches = runtime.setdefault("caches", {})
    cache = caches.setdefault(bucket, {})
    cache[key] = _CACHE_MISS if value is None else _copy_cached_value(value)
    return value


def hot_runtime_cache_get(bucket: str, key: str) -> Tuple[bool, Any]:
    with _HOT_CACHE_LOCK:
        cache = _HOT_CACHES.setdefault(bucket, {})
        row = cache.get(key)
        if row is None:
            return False, None
        expires_at, value = row
        if expires_at <= time.time():
            cache.pop(key, None)
            return False, None
        if value is _CACHE_MISS:
            return True, None
        return True, _copy_cached_value(value)


def hot_runtime_cache_put(bucket: str, key: str, value: Any) -> Any:
    with _HOT_CACHE_LOCK:
        _HOT_CACHES.setdefault(bucket, {})[key] = (
            time.time() + float(_HOT_CACHE_TTL_SECONDS),
            _CACHE_MISS if value is None else _copy_cached_value(value),
        )
    return value


def hot_runtime_cache_invalidate(bucket: str, key: str) -> None:
    with _HOT_CACHE_LOCK:
        _HOT_CACHES.setdefault(bucket, {}).pop(key, None)


def hot_runtime_cache_clear(bucket: str) -> None:
    with _HOT_CACHE_LOCK:
        _HOT_CACHES.setdefault(bucket, {}).clear()


def ensure_feature_schema(server: Any) -> None:
    global _SQLITE_SCHEMA_READY, _POSTGRES_SCHEMA_READY
    with _SCHEMA_LOCK:
        if (
            persistent_store_reads_enabled()
            and db_available()
            and not _POSTGRES_SCHEMA_READY
        ):
            try:
                with get_connection() as connection:
                    if connection is not None:
                        with connection.cursor() as cursor:
                            cursor.execute(_POSTGRES_SCHEMA_SQL)
                _POSTGRES_SCHEMA_READY = True
            except Exception:
                pass
        if not _SQLITE_SCHEMA_READY:
            connection = open_recommendation_store_connection(server)
            try:
                for statement in _SQLITE_SCHEMA_SQL:
                    connection.execute(statement)
                connection.commit()
                _SQLITE_SCHEMA_READY = True
            finally:
                connection.close()


def script_bucket(text: str) -> str:
    for bucket, pattern in _SCRIPT_PATTERNS.items():
        if pattern.search(text or ""):
            return bucket
    if re.search(r"[A-Za-z]", text or ""):
        return "latin"
    return "unknown"


def extract_year(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value if 1900 <= value <= 2099 else None
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"(19|20)\d{2}", text)
    if not match:
        return None
    year = int(match.group(0))
    return year if 1900 <= year <= 2099 else None


def era_bucket(year: Optional[int]) -> str:
    if year is None:
        return ""
    return f"{(year // 10) * 10}s"


def _freshness_from_year(year: Optional[int]) -> float:
    if year is None:
        return 0.35
    age = max(time.gmtime().tm_year - year, 0)
    return max(0.0, min(1.0, 1.0 - (age / 40.0)))


def _track_text(server: Any, track: Dict[str, Any]) -> str:
    return " ".join(
        part
        for part in [
            server._recommendation_trim_text(track.get("title")),
            server._recommendation_trim_text(
                track.get("channel") or track.get("artist") or track.get("author")
            ),
            server._recommendation_trim_text(track.get("album")),
        ]
        if part
    )


def _prepare_track_feature_input(server: Any, track: Dict[str, Any]) -> Dict[str, Any]:
    raw_track = dict(track or {})
    normalized_track = server.normalize_recommendation_track(raw_track) or dict(raw_track)
    merged_track = dict(normalized_track)
    for key, value in raw_track.items():
        if key in ("year", "release_year", "release_date", "releaseDate", "published"):
            if value not in (None, ""):
                merged_track[key] = value
            continue
        if key not in merged_track or merged_track.get(key) in (None, "", 0, []):
            merged_track[key] = value
    return merged_track


def _artist_text(server: Any, artist: Dict[str, Any]) -> str:
    return " ".join(
        part
        for part in [
            server._recommendation_trim_text(artist.get("name")),
            server._recommendation_trim_text(artist.get("description")),
        ]
        if part
    )


def _album_text(server: Any, album: Dict[str, Any]) -> str:
    return " ".join(
        part
        for part in [
            server._recommendation_trim_text(album.get("title")),
            server._recommendation_trim_text(album.get("artist")),
            server._recommendation_trim_text(album.get("year")),
        ]
        if part
    )


def _infer_type_tags(text: str) -> List[str]:
    tags: List[str] = []
    for tag, pattern in _TYPE_PATTERNS.items():
        if pattern.search(text or ""):
            tags.append(tag)
    return _normalize_sequence(tags)


def _infer_genre_bundle(text: str) -> Tuple[str, List[str], str]:
    scores: Counter[Tuple[str, str]] = Counter()
    for genre, subgenre, pattern in _GENRE_RULES:
        if pattern.search(text or ""):
            scores[(genre, subgenre)] += 1
    if not scores:
        return "", [], ""
    ranked = scores.most_common()
    primary_genre, subgenre = ranked[0][0]
    secondary = _normalize_sequence(
        [genre for (genre, _sub), _score in ranked[1:4] if genre != primary_genre]
    )
    return primary_genre, secondary, subgenre


def _infer_language_region(text: str, script: str) -> Tuple[str, str]:
    for language, region, pattern in _LANGUAGE_HINTS:
        if pattern.search(text or ""):
            return language, region
    if script == "devanagari":
        return "hindi", "india"
    if script == "arabic":
        return "arabic", "mena"
    if script == "cyrillic":
        return "cyrillic", "cis"
    if script in {"han", "kana", "hangul"}:
        return "cjk", "east_asia"
    if script == "latin":
        return "english", "global"
    return "unknown", "unknown"


def _infer_mood_axes(primary_genre: str, subgenre: str, type_tags: Sequence[str]) -> Dict[str, float]:
    calmness = 0.35
    energy = 0.5
    darkness = 0.35
    softness = 0.45
    drive = 0.5
    if primary_genre in {"rock", "metal"}:
        energy += 0.28
        drive += 0.25
        darkness += 0.1
        softness -= 0.18
    if subgenre in {"ambient", "folk", "blues", "soul"}:
        calmness += 0.22
        softness += 0.18
        energy -= 0.12
    if primary_genre in {"electronic", "hip_hop"}:
        drive += 0.16
        energy += 0.12
    if primary_genre == "devotional":
        calmness += 0.3
        softness += 0.22
        energy -= 0.18
    if "acoustic" in type_tags or "instrumental" in type_tags:
        calmness += 0.24
        softness += 0.2
        energy -= 0.16
    if "live" in type_tags:
        energy += 0.12
        drive += 0.1
    if "remix" in type_tags:
        energy += 0.14
        drive += 0.12
        calmness -= 0.1
    return {
        "calmness": round(max(0.0, min(calmness, 1.0)), 4),
        "energy": round(max(0.0, min(energy, 1.0)), 4),
        "darkness": round(max(0.0, min(darkness, 1.0)), 4),
        "softness": round(max(0.0, min(softness, 1.0)), 4),
        "drive": round(max(0.0, min(drive, 1.0)), 4),
    }


def _scene_clusters(primary_genre: str, subgenre: str, era: str, region: str) -> List[str]:
    clusters: List[str] = []
    if primary_genre:
        clusters.append(f"genre:{primary_genre}")
    if subgenre:
        clusters.append(f"subgenre:{subgenre}")
    if era:
        clusters.append(f"era:{era}")
    if primary_genre and era:
        clusters.append(f"scene:{primary_genre}:{era}")
    if primary_genre and region and region != "unknown":
        clusters.append(f"scene:{primary_genre}:{region}")
    return _normalize_sequence(clusters)


def _track_entity_key(server: Any, track: Dict[str, Any]) -> str:
    track_id = server._recommendation_trim_text(track.get("id"))
    return track_id or canonical_title_artist_identity(track) or server._recommendation_track_signature(track)


def _artist_entity_key(server: Any, artist: Dict[str, Any] | str) -> str:
    payload = dict(artist) if isinstance(artist, dict) else {"name": str(artist or "")}
    artist_id = server._recommendation_trim_text(payload.get("id"))
    return artist_id or canonical_artist_identity(payload)


def _album_entity_key(server: Any, album: Dict[str, Any]) -> str:
    album_id = server._recommendation_trim_text(album.get("id"))
    if album_id:
        return album_id
    return canonical_album_identity(album)


def derive_track_feature(server: Any, track: Dict[str, Any]) -> Dict[str, Any]:
    normalized_track = _prepare_track_feature_input(server, track)
    text = _track_text(server, normalized_track)
    script = script_bucket(text)
    year = None
    for key in ("year", "release_year", "release_date", "releaseDate", "published"):
        year = extract_year(normalized_track.get(key))
        if year is not None:
            break
    era = era_bucket(year)
    type_tags = _infer_type_tags(text)
    primary_genre, secondary_genres, subgenre = _infer_genre_bundle(text)
    language, region = _infer_language_region(text, script)
    confidence = 0.25 + (0.25 if primary_genre else 0.0) + (0.15 if year else 0.0) + (0.12 if script != "unknown" else 0.0) + (0.08 if type_tags else 0.0)
    return {
        "entity_type": "track",
        "feature_version": CATALOG_FEATURE_VERSION,
        "scene_graph_version": SCENE_GRAPH_VERSION,
        "track_id": server._recommendation_trim_text(normalized_track.get("id")),
        "artist_key": server._normalize_text(normalized_track.get("channel") or normalized_track.get("artist") or normalized_track.get("author") or ""),
        "album_key": server._normalize_text(normalized_track.get("album") or ""),
        "title_key": server._normalize_text(normalized_track.get("title") or ""),
        "primary_genre": primary_genre,
        "secondary_genres": secondary_genres,
        "subgenre": subgenre,
        "release_year": year,
        "era_bucket": era,
        "language": language,
        "script": script,
        "region": region,
        "scene_cluster_ids": _scene_clusters(primary_genre, subgenre, era, region),
        "peer_artist_ids": [],
        "track_type_tags": type_tags,
        "mood_axes": _infer_mood_axes(primary_genre, subgenre, type_tags),
        "popularity": 0.45,
        "freshness": _freshness_from_year(year),
        "confidence": round(max(0.0, min(confidence, 0.95)), 4),
        "source_metadata": {
            "title": normalized_track.get("title") or "",
            "artist": normalized_track.get("channel") or normalized_track.get("artist") or "",
            "album": normalized_track.get("album") or "",
        },
    }


def derive_artist_feature(server: Any, artist: Dict[str, Any] | str) -> Dict[str, Any]:
    payload = dict(artist) if isinstance(artist, dict) else {"name": str(artist or "")}
    text = _artist_text(server, payload)
    script = script_bucket(text)
    primary_genre, secondary_genres, subgenre = _infer_genre_bundle(text)
    language, region = _infer_language_region(text, script)
    peer_artist_ids = _normalize_sequence(
        [
            server._recommendation_trim_text((item or {}).get("id"))
            or server._normalize_text((item or {}).get("name") or "")
            for item in (payload.get("related_artists") or [])
            if isinstance(item, dict)
        ]
    )
    return {
        "entity_type": "artist",
        "feature_version": CATALOG_FEATURE_VERSION,
        "scene_graph_version": SCENE_GRAPH_VERSION,
        "artist_id": server._recommendation_trim_text(payload.get("id")),
        "artist_key": _artist_entity_key(server, payload),
        "name": server._recommendation_trim_text(payload.get("name")),
        "primary_genre": primary_genre,
        "secondary_genres": secondary_genres,
        "subgenre": subgenre,
        "active_era": "",
        "language": language,
        "script": script,
        "region": region,
        "scene_cluster_ids": _scene_clusters(primary_genre, subgenre, "", region),
        "peer_artist_ids": peer_artist_ids,
        "popularity": 0.5,
        "confidence": round(0.3 + (0.2 if primary_genre else 0.0) + (0.12 if peer_artist_ids else 0.0), 4),
        "source_metadata": {"description": payload.get("description") or ""},
    }


def derive_album_feature(server: Any, album: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(album or {})
    text = _album_text(server, payload)
    script = script_bucket(text)
    primary_genre, secondary_genres, subgenre = _infer_genre_bundle(text)
    year = extract_year(payload.get("year"))
    era = era_bucket(year)
    language, region = _infer_language_region(text, script)
    return {
        "entity_type": "album",
        "feature_version": CATALOG_FEATURE_VERSION,
        "scene_graph_version": SCENE_GRAPH_VERSION,
        "album_id": server._recommendation_trim_text(payload.get("id")),
        "album_key": _album_entity_key(server, payload),
        "title_key": server._normalize_text(payload.get("title") or ""),
        "artist_key": server._normalize_text(payload.get("artist") or ""),
        "primary_genre": primary_genre,
        "secondary_genres": secondary_genres,
        "subgenre": subgenre,
        "release_year": year,
        "era_bucket": era,
        "language": language,
        "script": script,
        "region": region,
        "scene_cluster_ids": _scene_clusters(primary_genre, subgenre, era, region),
        "popularity": 0.46,
        "freshness": _freshness_from_year(year),
        "confidence": round(0.28 + (0.2 if primary_genre else 0.0) + (0.14 if year else 0.0), 4),
        "source_metadata": {
            "title": payload.get("title") or "",
            "artist": payload.get("artist") or "",
        },
    }


def _load_pg_feature(entity_type: str, entity_key: str, external_id: str = "") -> Optional[Dict[str, Any]]:
    if not db_available():
        return None
    try:
        with get_connection() as connection:
            if connection is None:
                return None
            with connection.cursor() as cursor:
                if external_id:
                    cursor.execute(
                        """
                        SELECT payload, source_kind, confidence, feature_version, scene_graph_version
                        FROM recommendation_catalog_features
                        WHERE entity_type = %s AND (entity_key = %s OR external_id = %s)
                        ORDER BY CASE WHEN entity_key = %s THEN 0 ELSE 1 END
                        LIMIT 1
                        """,
                        [entity_type, entity_key, external_id, entity_key],
                    )
                else:
                    cursor.execute(
                        """
                        SELECT payload, source_kind, confidence, feature_version, scene_graph_version
                        FROM recommendation_catalog_features
                        WHERE entity_type = %s AND entity_key = %s
                        LIMIT 1
                        """,
                        [entity_type, entity_key],
                    )
                row = cursor.fetchone()
                if not row:
                    return None
                payload = _json_loads(row[0])
                payload.setdefault("source_kind", row[1] or "")
                payload.setdefault("confidence", float(row[2] or 0.0))
                payload.setdefault("feature_version", row[3] or CATALOG_FEATURE_VERSION)
                payload.setdefault("scene_graph_version", row[4] or SCENE_GRAPH_VERSION)
                return payload
    except Exception:
        return None


def _load_sqlite_feature(server: Any, entity_type: str, entity_key: str, external_id: str = "") -> Optional[Dict[str, Any]]:
    connection = open_recommendation_store_connection(server)
    try:
        if external_id:
            row = connection.execute(
                """
                SELECT payload_json, source_kind, confidence, feature_version, scene_graph_version
                FROM recommendation_catalog_features
                WHERE entity_type = ? AND (entity_key = ? OR external_id = ?)
                ORDER BY CASE WHEN entity_key = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                [entity_type, entity_key, external_id, entity_key],
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT payload_json, source_kind, confidence, feature_version, scene_graph_version
                FROM recommendation_catalog_features
                WHERE entity_type = ? AND entity_key = ?
                LIMIT 1
                """,
                [entity_type, entity_key],
            ).fetchone()
        if row is None:
            return None
        payload = _json_loads(row["payload_json"])
        payload.setdefault("source_kind", row["source_kind"] or "")
        payload.setdefault("confidence", float(row["confidence"] or 0.0))
        payload.setdefault("feature_version", row["feature_version"] or CATALOG_FEATURE_VERSION)
        payload.setdefault("scene_graph_version", row["scene_graph_version"] or SCENE_GRAPH_VERSION)
        return payload
    finally:
        connection.close()


def load_catalog_feature(
    server: Any,
    *,
    entity_type: str,
    entity_key: str,
    external_id: str = "",
) -> Optional[Dict[str, Any]]:
    normalized_key = str(entity_key or "").strip()
    if not normalized_key:
        return None
    cache_key = f"{entity_type}:{normalized_key}:{str(external_id or '').strip()}"
    cached, payload = request_runtime_cache_get("catalog", cache_key)
    if cached:
        return payload
    cached, payload = hot_runtime_cache_get("catalog", cache_key)
    if cached:
        request_runtime_cache_put("catalog", cache_key, payload)
        return payload
    ensure_feature_schema(server)
    payload = None
    if persistent_store_reads_enabled():
        payload = _load_pg_feature(entity_type, normalized_key, external_id=external_id)
    if payload is not None:
        hot_runtime_cache_put("catalog", cache_key, payload)
        request_runtime_cache_put("catalog", cache_key, payload)
        return payload
    payload = _load_sqlite_feature(server, entity_type, normalized_key, external_id=external_id)
    hot_runtime_cache_put("catalog", cache_key, payload)
    request_runtime_cache_put("catalog", cache_key, payload)
    return payload


def _store_pg_feature(
    *,
    entity_type: str,
    entity_key: str,
    external_id: str,
    feature_version: str,
    scene_graph_version: str,
    source_kind: str,
    confidence: float,
    payload: Dict[str, Any],
) -> bool:
    if not db_available():
        return False
    try:
        with get_connection() as connection:
            if connection is None:
                return False
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO recommendation_catalog_features(
                        entity_type, entity_key, external_id, feature_version,
                        scene_graph_version, source_kind, confidence, payload, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                    ON CONFLICT (entity_type, entity_key)
                    DO UPDATE SET
                        external_id = EXCLUDED.external_id,
                        feature_version = EXCLUDED.feature_version,
                        scene_graph_version = EXCLUDED.scene_graph_version,
                        source_kind = EXCLUDED.source_kind,
                        confidence = EXCLUDED.confidence,
                        payload = EXCLUDED.payload,
                        updated_at = now()
                    """,
                    [
                        entity_type,
                        entity_key,
                        external_id,
                        feature_version,
                        scene_graph_version,
                        source_kind,
                        float(confidence or 0.0),
                        _json_dumps(payload),
                    ],
                )
        return True
    except Exception:
        return False


def _store_sqlite_feature(
    server: Any,
    *,
    entity_type: str,
    entity_key: str,
    external_id: str,
    feature_version: str,
    scene_graph_version: str,
    source_kind: str,
    confidence: float,
    payload: Dict[str, Any],
) -> None:
    connection = open_recommendation_store_connection(server)
    try:
        connection.execute(
            """
            INSERT INTO recommendation_catalog_features(
                entity_type, entity_key, external_id, feature_version,
                scene_graph_version, source_kind, confidence, payload_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_type, entity_key)
            DO UPDATE SET
                external_id = excluded.external_id,
                feature_version = excluded.feature_version,
                scene_graph_version = excluded.scene_graph_version,
                source_kind = excluded.source_kind,
                confidence = excluded.confidence,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            [
                entity_type,
                entity_key,
                external_id,
                feature_version,
                scene_graph_version,
                source_kind,
                float(confidence or 0.0),
                _json_dumps(payload),
                float(time.time()),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def store_catalog_feature(
    server: Any,
    *,
    entity_type: str,
    entity_key: str,
    external_id: str = "",
    payload: Dict[str, Any],
    source_kind: str = "derived_fallback",
    confidence: float = 0.5,
) -> Dict[str, Any]:
    normalized_key = str(entity_key or "").strip()
    if not normalized_key:
        return dict(payload or {})
    ensure_feature_schema(server)
    feature_payload = dict(payload or {})
    feature_payload.setdefault("feature_version", CATALOG_FEATURE_VERSION)
    feature_payload.setdefault("scene_graph_version", SCENE_GRAPH_VERSION)
    feature_payload.setdefault("source_kind", source_kind)
    feature_payload.setdefault("confidence", float(confidence or 0.0))
    pg_stored = _store_pg_feature(
        entity_type=entity_type,
        entity_key=normalized_key,
        external_id=external_id,
        feature_version=str(feature_payload.get("feature_version") or CATALOG_FEATURE_VERSION),
        scene_graph_version=str(feature_payload.get("scene_graph_version") or SCENE_GRAPH_VERSION),
        source_kind=str(feature_payload.get("source_kind") or source_kind),
        confidence=float(feature_payload.get("confidence") or confidence or 0.0),
        payload=feature_payload,
    )
    _store_sqlite_feature(
        server,
        entity_type=entity_type,
        entity_key=normalized_key,
        external_id=external_id,
        feature_version=str(feature_payload.get("feature_version") or CATALOG_FEATURE_VERSION),
        scene_graph_version=str(feature_payload.get("scene_graph_version") or SCENE_GRAPH_VERSION),
        source_kind=str(feature_payload.get("source_kind") or source_kind),
        confidence=float(feature_payload.get("confidence") or confidence or 0.0),
        payload=feature_payload,
    )
    cache_key = f"{entity_type}:{normalized_key}:{str(external_id or '').strip()}"
    hot_runtime_cache_put("catalog", cache_key, feature_payload)
    request_runtime_cache_put("catalog", cache_key, feature_payload)
    return feature_payload


def _feature_richer(stored: Dict[str, Any], derived: Dict[str, Any]) -> bool:
    stored_confidence = float(stored.get("confidence") or 0.0)
    derived_confidence = float(derived.get("confidence") or 0.0)
    if derived_confidence > stored_confidence + 0.05:
        return True
    stored_metadata = dict(stored.get("source_metadata") or {})
    derived_metadata = dict(derived.get("source_metadata") or {})
    if any(
        str(stored_metadata.get(key) or "").strip().lower()
        != str(derived_metadata.get(key) or "").strip().lower()
        for key in ("title", "artist", "album")
        if derived_metadata.get(key)
    ):
        return True
    if str(stored.get("release_year") or "") != str(derived.get("release_year") or "") and derived.get("release_year"):
        return True
    for key in (
        "primary_genre",
        "subgenre",
        "release_year",
        "era_bucket",
        "language",
        "script",
        "region",
    ):
        if not stored.get(key) and derived.get(key):
            return True
    for key in (
        "secondary_genres",
        "scene_cluster_ids",
        "peer_artist_ids",
        "track_type_tags",
    ):
        if not list(stored.get(key) or []) and list(derived.get(key) or []):
            return True
    return False


def get_track_feature(
    server: Any,
    track: Dict[str, Any],
    *,
    persist: bool = False,
) -> Dict[str, Any]:
    normalized_track = _prepare_track_feature_input(server, track)
    track_id = server._recommendation_trim_text(normalized_track.get("id"))
    entity_key = _track_entity_key(server, normalized_track)
    stored = load_catalog_feature(server, entity_type="track", entity_key=entity_key, external_id=track_id)
    derived = derive_track_feature(server, normalized_track)
    if stored is not None:
        if _feature_richer(stored, derived):
            if persist:
                return store_catalog_feature(
                    server,
                    entity_type="track",
                    entity_key=entity_key,
                    external_id=track_id,
                    payload=derived,
                    source_kind="derived_refresh",
                    confidence=float(derived.get("confidence") or 0.0),
                )
            refreshed = dict(derived)
            refreshed["source_kind"] = "derived_refresh"
            return refreshed
        return stored
    if persist:
        return store_catalog_feature(
            server,
            entity_type="track",
            entity_key=entity_key,
            external_id=track_id,
            payload=derived,
            source_kind="derived_fallback",
            confidence=float(derived.get("confidence") or 0.0),
        )
    missing = dict(derived)
    missing["source_kind"] = "derived_fallback"
    return missing


def get_artist_feature(
    server: Any,
    artist: Dict[str, Any] | str,
    *,
    persist: bool = False,
) -> Dict[str, Any]:
    payload = dict(artist) if isinstance(artist, dict) else {"name": str(artist or "")}
    artist_id = server._recommendation_trim_text(payload.get("id"))
    entity_key = _artist_entity_key(server, payload)
    stored = load_catalog_feature(server, entity_type="artist", entity_key=entity_key, external_id=artist_id)
    derived = derive_artist_feature(server, payload)
    if stored is not None:
        if _feature_richer(stored, derived):
            if persist:
                return store_catalog_feature(
                    server,
                    entity_type="artist",
                    entity_key=entity_key,
                    external_id=artist_id,
                    payload=derived,
                    source_kind="derived_refresh",
                    confidence=float(derived.get("confidence") or 0.0),
                )
            refreshed = dict(derived)
            refreshed["source_kind"] = "derived_refresh"
            return refreshed
        return stored
    if persist:
        return store_catalog_feature(
            server,
            entity_type="artist",
            entity_key=entity_key,
            external_id=artist_id,
            payload=derived,
            source_kind="derived_fallback",
            confidence=float(derived.get("confidence") or 0.0),
        )
    missing = dict(derived)
    missing["source_kind"] = "derived_fallback"
    return missing


def get_album_feature(
    server: Any,
    album: Dict[str, Any],
    *,
    persist: bool = False,
) -> Dict[str, Any]:
    payload = dict(album or {})
    album_id = server._recommendation_trim_text(payload.get("id"))
    entity_key = _album_entity_key(server, payload)
    stored = load_catalog_feature(server, entity_type="album", entity_key=entity_key, external_id=album_id)
    derived = derive_album_feature(server, payload)
    if stored is not None:
        if _feature_richer(stored, derived):
            if persist:
                return store_catalog_feature(
                    server,
                    entity_type="album",
                    entity_key=entity_key,
                    external_id=album_id,
                    payload=derived,
                    source_kind="derived_refresh",
                    confidence=float(derived.get("confidence") or 0.0),
                )
            refreshed = dict(derived)
            refreshed["source_kind"] = "derived_refresh"
            return refreshed
        return stored
    if persist:
        return store_catalog_feature(
            server,
            entity_type="album",
            entity_key=entity_key,
            external_id=album_id,
            payload=derived,
            source_kind="derived_fallback",
            confidence=float(derived.get("confidence") or 0.0),
        )
    missing = dict(derived)
    missing["source_kind"] = "derived_fallback"
    return missing


def _load_pg_taste_profile(user_scope_id: str) -> Optional[Dict[str, Any]]:
    if not db_available():
        return None
    try:
        with get_connection() as connection:
            if connection is None:
                return None
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payload, profile_version, source_signature
                    FROM recommendation_taste_profiles
                    WHERE user_scope_id = %s
                    LIMIT 1
                    """,
                    [user_scope_id],
                )
                row = cursor.fetchone()
                if not row:
                    return None
                payload = _json_loads(row[0])
                payload.setdefault("profile_version", row[1] or TASTE_PROFILE_VERSION)
                payload.setdefault("source_signature", row[2] or "")
                return payload
    except Exception:
        return None


def _load_sqlite_taste_profile(server: Any, user_scope_id: str) -> Optional[Dict[str, Any]]:
    connection = open_recommendation_store_connection(server)
    try:
        row = connection.execute(
            """
            SELECT payload_json, profile_version, source_signature
            FROM recommendation_taste_profiles
            WHERE user_scope_id = ?
            LIMIT 1
            """,
            [user_scope_id],
        ).fetchone()
        if row is None:
            return None
        payload = _json_loads(row["payload_json"])
        payload.setdefault("profile_version", row["profile_version"] or TASTE_PROFILE_VERSION)
        payload.setdefault("source_signature", row["source_signature"] or "")
        return payload
    finally:
        connection.close()


def load_taste_profile(server: Any, *, user_scope_id: str) -> Optional[Dict[str, Any]]:
    normalized_scope = str(user_scope_id or "").strip()
    if not normalized_scope:
        return None
    cached, payload = request_runtime_cache_get("taste", normalized_scope)
    if cached:
        return payload
    cached, payload = hot_runtime_cache_get("taste", normalized_scope)
    if cached:
        request_runtime_cache_put("taste", normalized_scope, payload)
        return payload
    ensure_feature_schema(server)
    payload = None
    if persistent_store_reads_enabled():
        payload = _load_pg_taste_profile(normalized_scope)
    if payload is not None:
        hot_runtime_cache_put("taste", normalized_scope, payload)
        request_runtime_cache_put("taste", normalized_scope, payload)
        return payload
    payload = _load_sqlite_taste_profile(server, normalized_scope)
    hot_runtime_cache_put("taste", normalized_scope, payload)
    request_runtime_cache_put("taste", normalized_scope, payload)
    return payload


def _store_pg_taste_profile(
    *,
    user_scope_id: str,
    profile_version: str,
    source_signature: str,
    payload: Dict[str, Any],
) -> bool:
    if not db_available():
        return False
    try:
        with get_connection() as connection:
            if connection is None:
                return False
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO recommendation_taste_profiles(
                        user_scope_id, profile_version, source_signature, payload, updated_at
                    )
                    VALUES (%s, %s, %s, %s::jsonb, now())
                    ON CONFLICT (user_scope_id)
                    DO UPDATE SET
                        profile_version = EXCLUDED.profile_version,
                        source_signature = EXCLUDED.source_signature,
                        payload = EXCLUDED.payload,
                        updated_at = now()
                    """,
                    [
                        user_scope_id,
                        profile_version,
                        source_signature,
                        _json_dumps(payload),
                    ],
                )
        return True
    except Exception:
        return False


def _store_sqlite_taste_profile(
    server: Any,
    *,
    user_scope_id: str,
    profile_version: str,
    source_signature: str,
    payload: Dict[str, Any],
) -> None:
    connection = open_recommendation_store_connection(server)
    try:
        connection.execute(
            """
            INSERT INTO recommendation_taste_profiles(
                user_scope_id, profile_version, source_signature, payload_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_scope_id)
            DO UPDATE SET
                profile_version = excluded.profile_version,
                source_signature = excluded.source_signature,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            [
                user_scope_id,
                profile_version,
                source_signature,
                _json_dumps(payload),
                float(time.time()),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def store_taste_profile(
    server: Any,
    *,
    user_scope_id: str,
    payload: Dict[str, Any],
    source_signature: str,
) -> Dict[str, Any]:
    normalized_scope = str(user_scope_id or "").strip()
    if not normalized_scope:
        return dict(payload or {})
    ensure_feature_schema(server)
    stored_payload = dict(payload or {})
    stored_payload.setdefault("profile_version", TASTE_PROFILE_VERSION)
    stored_payload.setdefault("catalog_feature_version", CATALOG_FEATURE_VERSION)
    stored_payload.setdefault("scene_graph_version", SCENE_GRAPH_VERSION)
    stored_payload["source_signature"] = source_signature or ""
    _store_pg_taste_profile(
        user_scope_id=normalized_scope,
        profile_version=str(stored_payload.get("profile_version") or TASTE_PROFILE_VERSION),
        source_signature=str(source_signature or ""),
        payload=stored_payload,
    )
    _store_sqlite_taste_profile(
        server,
        user_scope_id=normalized_scope,
        profile_version=str(stored_payload.get("profile_version") or TASTE_PROFILE_VERSION),
        source_signature=str(source_signature or ""),
        payload=stored_payload,
    )
    hot_runtime_cache_put("taste", normalized_scope, stored_payload)
    request_runtime_cache_put("taste", normalized_scope, stored_payload)
    return stored_payload


def delete_taste_profile(server: Any, *, user_scope_id: str) -> None:
    normalized_scope = str(user_scope_id or "").strip()
    if not normalized_scope:
        return
    ensure_feature_schema(server)
    if db_available():
        try:
            with get_connection() as connection:
                if connection is not None:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "DELETE FROM recommendation_taste_profiles WHERE user_scope_id = %s",
                            [normalized_scope],
                        )
        except Exception:
            pass
    connection = open_recommendation_store_connection(server)
    try:
        connection.execute(
            "DELETE FROM recommendation_taste_profiles WHERE user_scope_id = ?",
            [normalized_scope],
        )
        connection.commit()
    finally:
        connection.close()
    hot_runtime_cache_invalidate("taste", normalized_scope)
    request_runtime_cache_put("taste", normalized_scope, None)


def _cleanup_pg_feedback(user_scope_id: str, now_ts: float) -> None:
    if not db_available():
        return
    try:
        with get_connection() as connection:
            if connection is None:
                return
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM recommendation_negative_feedback
                    WHERE user_scope_id = %s AND expires_at > 0 AND expires_at <= %s
                    """,
                    [user_scope_id, float(now_ts)],
                )
    except Exception:
        return


def _cleanup_sqlite_feedback(server: Any, user_scope_id: str, now_ts: float) -> None:
    connection = open_recommendation_store_connection(server)
    try:
        connection.execute(
            """
            DELETE FROM recommendation_negative_feedback
            WHERE user_scope_id = ? AND expires_at > 0 AND expires_at <= ?
            """,
            [user_scope_id, float(now_ts)],
        )
        connection.commit()
    finally:
        connection.close()


def _load_pg_feedback_rows(user_scope_id: str) -> List[Dict[str, Any]]:
    if not db_available():
        return []
    try:
        with get_connection() as connection:
            if connection is None:
                return []
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT feedback_type, feedback_key, strength, expires_at, metadata
                    FROM recommendation_negative_feedback
                    WHERE user_scope_id = %s
                    ORDER BY strength DESC, updated_at DESC
                    """,
                    [user_scope_id],
                )
                rows = cursor.fetchall() or []
    except Exception:
        return []
    return [
        {
            "feedback_type": row[0] or "",
            "feedback_key": row[1] or "",
            "strength": float(row[2] or 0.0),
            "expires_at": float(row[3] or 0.0),
            "metadata": dict(row[4] or {}),
        }
        for row in rows
    ]


def _load_sqlite_feedback_rows(server: Any, user_scope_id: str) -> List[Dict[str, Any]]:
    connection = open_recommendation_store_connection(server)
    try:
        rows = connection.execute(
            """
            SELECT feedback_type, feedback_key, strength, expires_at, metadata_json
            FROM recommendation_negative_feedback
            WHERE user_scope_id = ?
            ORDER BY strength DESC, updated_at DESC
            """,
            [user_scope_id],
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "feedback_type": row["feedback_type"] or "",
            "feedback_key": row["feedback_key"] or "",
            "strength": float(row["strength"] or 0.0),
            "expires_at": float(row["expires_at"] or 0.0),
            "metadata": _json_loads(row["metadata_json"]),
        }
        for row in rows or []
    ]


def load_negative_feedback(server: Any, *, user_scope_id: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
    normalized_scope = str(user_scope_id or "").strip()
    if not normalized_scope:
        return {}
    cached, payload = request_runtime_cache_get("negative", normalized_scope)
    if cached:
        return payload or {}
    cached, payload = hot_runtime_cache_get("negative", normalized_scope)
    if cached:
        request_runtime_cache_put("negative", normalized_scope, payload or {})
        return payload or {}
    ensure_feature_schema(server)
    now_ts = float(time.time())
    if persistent_store_reads_enabled():
        _cleanup_pg_feedback(normalized_scope, now_ts)
        _cleanup_sqlite_feedback(server, normalized_scope, now_ts)
        rows = _load_pg_feedback_rows(normalized_scope) or _load_sqlite_feedback_rows(server, normalized_scope)
    else:
        rows = _load_sqlite_feedback_rows(server, normalized_scope)
    grouped: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in rows:
        feedback_type = str(row.get("feedback_type") or "").strip()
        feedback_key = str(row.get("feedback_key") or "").strip()
        if not feedback_type or not feedback_key:
            continue
        expires_at = float(row.get("expires_at") or 0.0)
        if expires_at > 0 and expires_at <= now_ts:
            continue
        grouped.setdefault(feedback_type, {})[feedback_key] = {
            "strength": float(row.get("strength") or 0.0),
            "expires_at": float(row.get("expires_at") or 0.0),
            "metadata": dict(row.get("metadata") or {}),
        }
    hot_runtime_cache_put("negative", normalized_scope, grouped)
    request_runtime_cache_put("negative", normalized_scope, grouped)
    return grouped


def _store_pg_feedback(
    *,
    user_scope_id: str,
    feedback_type: str,
    feedback_key: str,
    strength: float,
    expires_at: float,
    metadata: Dict[str, Any],
) -> bool:
    if not db_available():
        return False
    try:
        with get_connection() as connection:
            if connection is None:
                return False
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO recommendation_negative_feedback(
                        user_scope_id, feedback_type, feedback_key, strength, expires_at, metadata, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, now())
                    ON CONFLICT (user_scope_id, feedback_type, feedback_key)
                    DO UPDATE SET
                        strength = EXCLUDED.strength,
                        expires_at = EXCLUDED.expires_at,
                        metadata = EXCLUDED.metadata,
                        updated_at = now()
                    """,
                    [
                        user_scope_id,
                        feedback_type,
                        feedback_key,
                        float(strength or 0.0),
                        float(expires_at or 0.0),
                        _json_dumps(metadata),
                    ],
                )
        return True
    except Exception:
        return False


def _store_sqlite_feedback(
    server: Any,
    *,
    user_scope_id: str,
    feedback_type: str,
    feedback_key: str,
    strength: float,
    expires_at: float,
    metadata: Dict[str, Any],
) -> None:
    connection = open_recommendation_store_connection(server)
    try:
        connection.execute(
            """
            INSERT INTO recommendation_negative_feedback(
                user_scope_id, feedback_type, feedback_key, strength, expires_at, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_scope_id, feedback_type, feedback_key)
            DO UPDATE SET
                strength = excluded.strength,
                expires_at = excluded.expires_at,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            [
                user_scope_id,
                feedback_type,
                feedback_key,
                float(strength or 0.0),
                float(expires_at or 0.0),
                _json_dumps(metadata),
                float(time.time()),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def upsert_negative_feedback(
    server: Any,
    *,
    user_scope_id: str,
    feedback_type: str,
    feedback_key: str,
    strength: float,
    ttl_seconds: float,
    metadata: Dict[str, Any] | None = None,
) -> None:
    normalized_scope = str(user_scope_id or "").strip()
    normalized_type = str(feedback_type or "").strip()
    normalized_key = str(feedback_key or "").strip()
    if not normalized_scope or not normalized_type or not normalized_key:
        return
    ensure_feature_schema(server)
    expires_at = float(time.time()) + max(float(ttl_seconds or 0.0), 0.0)
    payload = dict(metadata or {})
    if not _store_pg_feedback(
        user_scope_id=normalized_scope,
        feedback_type=normalized_type,
        feedback_key=normalized_key,
        strength=float(strength or 0.0),
        expires_at=expires_at,
        metadata=payload,
    ):
        _store_sqlite_feedback(
            server,
            user_scope_id=normalized_scope,
            feedback_type=normalized_type,
            feedback_key=normalized_key,
            strength=float(strength or 0.0),
            expires_at=expires_at,
            metadata=payload,
        )
    else:
        _store_sqlite_feedback(
            server,
            user_scope_id=normalized_scope,
            feedback_type=normalized_type,
            feedback_key=normalized_key,
            strength=float(strength or 0.0),
            expires_at=expires_at,
            metadata=payload,
        )
    hot_runtime_cache_invalidate("negative", normalized_scope)
    request_runtime_cache_put("negative", normalized_scope, None)


def clear_negative_feedback(
    server: Any,
    *,
    user_scope_id: str,
    feedback_type: str,
    feedback_key: str,
) -> None:
    normalized_scope = str(user_scope_id or "").strip()
    normalized_type = str(feedback_type or "").strip()
    normalized_key = str(feedback_key or "").strip()
    if not normalized_scope or not normalized_type or not normalized_key:
        return
    ensure_feature_schema(server)
    if db_available():
        try:
            with get_connection() as connection:
                if connection is not None:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            DELETE FROM recommendation_negative_feedback
                            WHERE user_scope_id = %s AND feedback_type = %s AND feedback_key = %s
                            """,
                            [normalized_scope, normalized_type, normalized_key],
                        )
        except Exception:
            pass
    connection = open_recommendation_store_connection(server)
    try:
        connection.execute(
            """
            DELETE FROM recommendation_negative_feedback
            WHERE user_scope_id = ? AND feedback_type = ? AND feedback_key = ?
            """,
            [normalized_scope, normalized_type, normalized_key],
        )
        connection.commit()
    finally:
        connection.close()
    hot_runtime_cache_invalidate("negative", normalized_scope)
    request_runtime_cache_put("negative", normalized_scope, None)


def attenuate_negative_feedback(
    server: Any,
    *,
    user_scope_id: str,
    feedback_type: str,
    feedback_key: str,
    factor: float,
    floor: float = 0.12,
) -> None:
    rows = load_negative_feedback(server, user_scope_id=user_scope_id)
    entry = dict((rows.get(feedback_type) or {}).get(feedback_key) or {})
    if not entry:
        return
    updated_strength = float(entry.get("strength") or 0.0) * max(min(float(factor or 1.0), 1.0), 0.0)
    if updated_strength <= float(floor or 0.0):
        clear_negative_feedback(
            server,
            user_scope_id=user_scope_id,
            feedback_type=feedback_type,
            feedback_key=feedback_key,
        )
        return
    remaining_ttl = max(float(entry.get("expires_at") or 0.0) - float(time.time()), 60.0)
    upsert_negative_feedback(
        server,
        user_scope_id=user_scope_id,
        feedback_type=feedback_type,
        feedback_key=feedback_key,
        strength=updated_strength,
        ttl_seconds=remaining_ttl,
        metadata=dict(entry.get("metadata") or {}),
    )


def warm_feature_artifacts(
    server: Any,
    *,
    tracks: Sequence[Dict[str, Any]] | None = None,
    artists: Sequence[Dict[str, Any] | str] | None = None,
    albums: Sequence[Dict[str, Any]] | None = None,
) -> Dict[str, int]:
    counts = {"tracks": 0, "artists": 0, "albums": 0}
    for track in tracks or []:
        if not isinstance(track, dict):
            continue
        get_track_feature(server, track, persist=True)
        counts["tracks"] += 1
    for artist in artists or []:
        if isinstance(artist, (dict, str)):
            get_artist_feature(server, artist, persist=True)
            counts["artists"] += 1
    for album in albums or []:
        if not isinstance(album, dict):
            continue
        get_album_feature(server, album, persist=True)
        counts["albums"] += 1
    return counts
