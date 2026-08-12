from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import re
import threading
import time
from typing import Any, Callable, Dict

from ..domain.catalog import catalog_artwork_source_urls
from ..recommend.store_runtime import open_recommendation_store_connection

try:
    import boto3
    from botocore.config import Config
except Exception:  # pragma: no cover - optional production dependency
    boto3 = None
    Config = None


_CLIENTS: Dict[int, "ArtistArtworkCache | None"] = {}
_ENTITY_CLIENTS: Dict[int, "ArtistArtworkCache | None"] = {}
_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="auralis-artist-artwork",
)
_PENDING: set[str] = set()
_PENDING_LOCK = threading.Lock()
_UPDATE_LISTENERS: list[Callable[[Dict[str, Any]], None]] = []
_UPDATE_LISTENERS_LOCK = threading.Lock()
_ENTITY_UPDATE_LISTENERS: list[Callable[[Dict[str, Any]], None]] = []
_ENTITY_UPDATE_LISTENERS_LOCK = threading.Lock()
_ENTITY_INVALIDATION_LISTENERS: list[
    Callable[[Any, Dict[str, Any]], None]
] = []
_ENTITY_INVALIDATION_LISTENERS_LOCK = threading.Lock()
_ENTITY_RECORDS: Dict[str, tuple[Any, Dict[str, Any]]] = {}
_ENTITY_RECORDS_LOCK = threading.Lock()
_ENTITY_PENDING: set[str] = set()
_ENTITY_PENDING_LOCK = threading.Lock()
_TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")
_SOURCE_RETRY_SECONDS = 6 * 60 * 60
_ENTITY_SOURCE_ATTEMPT_LIMIT = 4


def _clean(value: Any) -> str:
    return str(value or "").strip()


def artist_artwork_token(canonical_artist_id: str) -> str:
    value = _clean(canonical_artist_id).casefold()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32] if value else ""


def artist_artwork_path(canonical_artist_id: str) -> str:
    token = artist_artwork_token(canonical_artist_id)
    return f"/artist_artwork/{token}" if token else ""


def _artwork_token_from_path(value: Any) -> str:
    path = _clean(value)
    token = path.removeprefix("/artist_artwork/") if path.startswith(
        "/artist_artwork/"
    ) else ""
    return token if _TOKEN_RE.match(token) else ""


def _http_artwork_url(value: Any) -> str:
    url = _clean(value)
    return url if url.startswith(("http://", "https://")) else ""


def _artist_artwork_source_urls(artist: Dict[str, Any]) -> list[str]:
    now = time.time()
    raw_failures = artist.get("artwork_source_failures") or {}
    failures = raw_failures if isinstance(raw_failures, dict) else {}

    def retry_ready(url: str) -> bool:
        try:
            failed_at = float(failures.get(url) or 0.0)
        except (TypeError, ValueError):
            failed_at = 0.0
        return now - failed_at >= _SOURCE_RETRY_SECONDS

    return [
        url
        for url in catalog_artwork_source_urls(artist, entity_type="artist")
        if retry_ready(url)
    ]


def _valid_image_bytes(data: bytes, content_type: str) -> bool:
    normalized_type = _clean(content_type).casefold()
    if normalized_type in {"image/jpeg", "image/jpg"}:
        return data.startswith(b"\xff\xd8\xff")
    if normalized_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if normalized_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if normalized_type == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    if normalized_type in {"image/avif", "image/heif", "image/heic"}:
        return len(data) >= 12 and data[4:8] == b"ftyp"
    return False


def _download_artwork(
    server: Any,
    source_url: str,
) -> tuple[bytes, str] | None:
    if not _http_artwork_url(source_url):
        return None
    try:
        response = server.upstream_http.get(
            source_url,
            timeout=(3.0, 8.0),
            stream=True,
        )
        response.raise_for_status()
        content_type = _clean(response.headers.get("Content-Type")).split(";", 1)[0]
        if not content_type.startswith("image/"):
            return None
        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > 2 * 1024 * 1024:
                return None
            chunks.append(chunk)
        data = b"".join(chunks)
        return (data, content_type) if _valid_image_bytes(data, content_type) else None
    except Exception:
        return None


def _artist_cache_identities(artist: Dict[str, Any]) -> list[str]:
    identities: list[str] = []

    def add(value: Any) -> None:
        identity = _clean(value)
        if identity and identity.casefold() not in {
            existing.casefold() for existing in identities
        }:
            identities.append(identity)

    provider_artist_id = _clean(
        artist.get("provider_artist_id")
        or artist.get("browseId")
        or artist.get("artist_id")
        or artist.get("id")
    )
    if provider_artist_id and not provider_artist_id.startswith(
        ("musicbrainz:artist:", "artist-name:", "derived:")
    ):
        add(f"provider:artist:{provider_artist_id.casefold()}")
    for value in (
        artist.get("artwork_cache_identity"),
        artist.get("canonical_artist_id"),
        artist.get("canonical_artist_key"),
    ):
        identity = _clean(value)
        if identity.startswith(("provider:artist:", "musicbrainz:artist:")):
            add(identity)
    normalized_name = re.sub(
        r"[^a-z0-9]+",
        " ",
        _clean(artist.get("normalized_name") or artist.get("name")).casefold(),
    ).strip()
    # A name is discovery evidence, not artwork ownership. Legacy name keys
    # remain readable only for records that have no provider/canonical
    # identity; a provider-backed homonym must never inherit those bytes.
    if normalized_name and not identities:
        add(f"artist-name:{normalized_name}")
        # Older cache entries used the bare normalized name.
        add(normalized_name)
    return identities


def attach_cached_artist_artwork(
    server: Any,
    artist: Dict[str, Any],
) -> Dict[str, Any]:
    """Attach an existing R2 object before attempting a live artwork lookup."""
    updated = dict(artist or {})
    thumbnail = _clean(updated.get("thumbnail"))
    source_urls = _artist_artwork_source_urls(updated)
    source_url = source_urls[0] if source_urls else ""
    if source_urls:
        updated["artwork_source_urls"] = source_urls
        updated["artwork_source_url"] = source_url
    else:
        updated.pop("artwork_source_urls", None)
        updated.pop("artwork_source_url", None)
    identities = _artist_cache_identities(updated)
    allowed_tokens = {
        artist_artwork_token(identity)
        for identity in identities
        if artist_artwork_token(identity)
    }
    current_token = _artwork_token_from_path(thumbnail)
    if current_token:
        cache = get_artist_artwork_cache(server)
        if (
            current_token in allowed_tokens
            and cache is not None
            and cache.head(current_token) is not None
        ):
            updated["artwork_cache_token"] = current_token
            return updated
        updated.pop("artwork_cache_token", None)
        if source_url:
            updated["thumbnail"] = source_url
        else:
            updated.pop("thumbnail", None)
    elif _http_artwork_url(thumbnail):
        if thumbnail in source_urls:
            source_url = thumbnail
            updated["artwork_source_url"] = source_url
        else:
            updated.pop("thumbnail", None)
    elif thumbnail:
        updated.pop("thumbnail", None)
    cache = get_artist_artwork_cache(server)
    if cache is None:
        if source_url:
            updated["thumbnail"] = source_url
        return updated
    persisted_token = _clean(updated.get("artwork_cache_token"))
    if (
        _TOKEN_RE.match(persisted_token)
        and persisted_token in allowed_tokens
        and cache.head(persisted_token) is not None
    ):
        updated["thumbnail"] = f"/artist_artwork/{persisted_token}"
        return updated
    if persisted_token:
        updated.pop("artwork_cache_token", None)
    for identity in identities:
        token = artist_artwork_token(identity)
        if not token or cache.head(token) is None:
            continue
        updated["artwork_cache_identity"] = identity
        updated["artwork_cache_token"] = token
        updated["thumbnail"] = f"/artist_artwork/{token}"
        return updated
    if source_url:
        updated["thumbnail"] = source_url
    return updated


def register_artist_metadata_listener(
    listener: Callable[[Dict[str, Any]], None],
) -> None:
    with _UPDATE_LISTENERS_LOCK:
        if listener not in _UPDATE_LISTENERS:
            _UPDATE_LISTENERS.append(listener)


def notify_artist_metadata_updated(artist: Dict[str, Any]) -> None:
    with _UPDATE_LISTENERS_LOCK:
        listeners = list(_UPDATE_LISTENERS)
    for listener in listeners:
        try:
            listener(dict(artist))
        except Exception:
            continue


class ArtistArtworkCache:
    def __init__(
        self,
        server: Any,
        *,
        prefix_setting: str = "AURALIS_ARTIST_ARTWORK_CACHE_PREFIX",
        default_prefix: str = "artist-artwork",
    ) -> None:
        if boto3 is None or Config is None:
            raise RuntimeError("boto3 dependency unavailable")
        bucket = _clean(getattr(server, "AURALIS_STREAM_CACHE_BUCKET", ""))
        account_id = _clean(getattr(server, "AURALIS_R2_ACCOUNT_ID", ""))
        access_key = _clean(getattr(server, "AURALIS_R2_ACCESS_KEY_ID", ""))
        secret_key = _clean(getattr(server, "AURALIS_R2_SECRET_ACCESS_KEY", ""))
        endpoint = _clean(getattr(server, "AURALIS_R2_ENDPOINT_URL", ""))
        if not endpoint and account_id:
            endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        if not bucket or not endpoint or not access_key or not secret_key:
            raise RuntimeError("R2 artist artwork cache is not configured")
        self.server = server
        self.bucket = bucket
        self.prefix = (
            _clean(getattr(server, prefix_setting, default_prefix))
            or default_prefix
        )
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 2}),
        )
        self._verified_tokens: set[str] = set()
        self._missing_tokens: set[str] = set()
        self._verified_tokens_lock = threading.Lock()

    def _key(self, token: str) -> str:
        return f"{self.prefix.rstrip('/')}/{token}.img"

    def head(self, token: str) -> Dict[str, Any] | None:
        if not _TOKEN_RE.match(token):
            return None
        with self._verified_tokens_lock:
            if token in self._verified_tokens:
                return {"content_type": "image/jpeg", "content_length": 1}
        try:
            response = self.client.head_object(
                Bucket=self.bucket,
                Key=self._key(token),
            )
        except Exception:
            return None
        content_length = int(response.get("ContentLength") or 0)
        if content_length <= 0:
            return None
        with self._verified_tokens_lock:
            self._verified_tokens.add(token)
            self._missing_tokens.discard(token)
        return {
            "content_type": _clean(response.get("ContentType")) or "image/jpeg",
            "content_length": content_length,
        }

    def read(self, token: str) -> tuple[bytes, str] | None:
        if not _TOKEN_RE.match(token):
            return None
        try:
            response = self.client.get_object(
                Bucket=self.bucket,
                Key=self._key(token),
            )
            body = response["Body"]
            try:
                data = body.read()
            finally:
                body.close()
        except Exception as exc:
            response = getattr(exc, "response", {}) or {}
            error = response.get("Error") or {}
            code = _clean(error.get("Code")).casefold()
            status = int(
                (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
                or 0
            )
            if code in {"404", "nosuchkey", "notfound"} or status == 404:
                with self._verified_tokens_lock:
                    self._missing_tokens.add(token)
                    self._verified_tokens.discard(token)
            return None
        if not data:
            with self._verified_tokens_lock:
                self._missing_tokens.add(token)
                self._verified_tokens.discard(token)
            return None
        with self._verified_tokens_lock:
            self._verified_tokens.add(token)
            self._missing_tokens.discard(token)
        return data, _clean(response.get("ContentType")) or "image/jpeg"

    def object_missing(self, token: str) -> bool:
        with self._verified_tokens_lock:
            return token in self._missing_tokens

    def store(
        self,
        *,
        token: str,
        source_url: str,
        canonical_artist_id: str = "",
        cache_identity: str = "",
    ) -> bool:
        if not _TOKEN_RE.match(token) or not source_url.startswith(("http://", "https://")):
            return False
        if self.head(token) is not None:
            return True
        try:
            downloaded = _download_artwork(self.server, source_url)
            if downloaded is None:
                return False
            data, content_type = downloaded
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(token),
                Body=data,
                ContentType=content_type,
                CacheControl="public, max-age=2592000, immutable",
                Metadata={
                    "cache-identity": (
                        cache_identity or canonical_artist_id
                    )[:512],
                    "cached-at": str(int(time.time())),
                },
            )
            with self._verified_tokens_lock:
                self._verified_tokens.add(token)
                self._missing_tokens.discard(token)
            return True
        except Exception:
            return False


def get_artist_artwork_cache(server: Any) -> ArtistArtworkCache | None:
    server = getattr(server, "raw", server)
    key = id(server)
    if key in _CLIENTS:
        return _CLIENTS[key]
    try:
        cache = ArtistArtworkCache(server)
    except Exception:
        _CLIENTS[key] = None
        return None
    _CLIENTS[key] = cache
    return cache


def entity_artwork_identity(
    item: Dict[str, Any],
    *,
    entity_type: str,
) -> str:
    normalized_type = _clean(entity_type).casefold()
    if normalized_type == "album":
        existing_identity = _clean(item.get("artwork_cache_identity"))
        if existing_identity.startswith("album:"):
            return existing_identity
        explicit_provider_id = _clean(
            item.get("provider_album_id")
            or item.get("browseId")
        )
        album_id = _clean(item.get("album_id"))
        item_id = _clean(item.get("id"))
        canonical_id = _clean(item.get("canonical_album_identity"))
        stable_id = explicit_provider_id or (
            album_id if album_id.startswith("MPRE") else ""
        ) or (
            item_id if item_id.startswith("MPRE") else ""
        ) or (
            canonical_id if canonical_id.startswith("MPRE") else ""
        )
    elif normalized_type == "playlist":
        stable_id = _clean(item.get("id") or item.get("browseId"))
    else:
        stable_id = ""
    return f"{normalized_type}:{stable_id}" if stable_id else ""


def entity_artwork_token(
    item: Dict[str, Any],
    *,
    entity_type: str,
) -> str:
    return artist_artwork_token(
        entity_artwork_identity(item, entity_type=entity_type)
    )


def entity_artwork_path(
    item: Dict[str, Any],
    *,
    entity_type: str,
) -> str:
    token = entity_artwork_token(item, entity_type=entity_type)
    return f"/entity_artwork/{token}" if token else ""


def _entity_artwork_source_urls(
    item: Dict[str, Any],
    *,
    entity_type: str,
) -> list[str]:
    now = time.time()
    try:
        retry_after = float(item.get("artwork_retry_after") or 0.0)
    except (TypeError, ValueError):
        retry_after = 0.0
    if retry_after > now:
        return []
    raw_failures = item.get("artwork_source_failures") or {}
    failures = raw_failures if isinstance(raw_failures, dict) else {}

    def retry_ready(url: str) -> bool:
        try:
            failed_at = float(failures.get(url) or 0.0)
        except (TypeError, ValueError):
            failed_at = 0.0
        return now - failed_at >= _SOURCE_RETRY_SECONDS

    return [
        url
        for url in catalog_artwork_source_urls(item, entity_type=entity_type)
        if retry_ready(url)
    ][:_ENTITY_SOURCE_ATTEMPT_LIMIT]


def get_entity_artwork_cache(server: Any) -> ArtistArtworkCache | None:
    server = getattr(server, "raw", server)
    key = id(server)
    if key in _ENTITY_CLIENTS:
        return _ENTITY_CLIENTS[key]
    try:
        cache = ArtistArtworkCache(
            server,
            prefix_setting="AURALIS_ENTITY_ARTWORK_CACHE_PREFIX",
            default_prefix="entity-artwork",
        )
    except Exception:
        _ENTITY_CLIENTS[key] = None
        return None
    _ENTITY_CLIENTS[key] = cache
    return cache


def attach_cached_entity_artwork(
    server: Any,
    item: Dict[str, Any],
    *,
    entity_type: str,
) -> Dict[str, Any]:
    """Attach only artwork whose persisted object has verified bytes."""
    server = getattr(server, "raw", server)
    updated = dict(item or {})
    normalized_type = _clean(entity_type).casefold()
    if normalized_type not in {"album", "playlist"}:
        return updated
    identity = entity_artwork_identity(updated, entity_type=normalized_type)
    token = artist_artwork_token(identity)
    if not identity or not token:
        updated.pop("thumbnail", None)
        return updated

    # A background callback may complete before the search snapshot is
    # materialized. Reattach the verified record by stable identity so the
    # subsequent snapshot does not report playlists=0 merely because its
    # incoming item lacked the callback's metadata.
    with _ENTITY_RECORDS_LOCK:
        remembered = next(
            (
                dict(record)
                for _token, (_server, record) in _ENTITY_RECORDS.items()
                if _server is server
                and _clean(record.get("artwork_cache_identity")) == identity
                and _clean(record.get("artwork_cache_status")).casefold() == "cached"
            ),
            None,
        )
    if remembered:
        updated = {**updated, **remembered}
    if not remembered:
        # Rehydrate the verified catalog payload after a backend restart. The
        # stable artwork identity is persisted with the entity record; this is
        # a single local lookup and does not perform an R2 HEAD in the served
        # snapshot path.
        try:
            connection = open_recommendation_store_connection(server)
            row = connection.execute(
                "SELECT payload_json FROM catalog_entities WHERE entity_type = ? AND entity_key = ? LIMIT 1",
                [normalized_type, _clean(updated.get("id") or updated.get("browseId"))],
            ).fetchone()
            connection.close()
            persisted = json.loads(row[0]) if row and row[0] else None
            if isinstance(persisted, dict) and _clean(persisted.get("artwork_cache_status")).casefold() == "cached":
                updated = {**updated, **persisted}
                remembered = persisted
        except Exception:
            pass

    existing_path = _clean(updated.get("thumbnail"))
    source_urls = catalog_artwork_source_urls(
        updated,
        entity_type=normalized_type,
    )
    if source_urls:
        updated["artwork_source_urls"] = source_urls
        updated["artwork_source_url"] = source_urls[0]
    persisted_token = _clean(updated.get("artwork_cache_token"))
    persisted_identity = _clean(updated.get("artwork_cache_identity"))
    persisted_status = _clean(updated.get("artwork_cache_status")).casefold()
    updated["artwork_entity_type"] = normalized_type
    updated["artwork_cache_identity"] = identity
    updated["artwork_cache_token"] = token
    cache = get_entity_artwork_cache(server)
    current_token = (
        existing_path.removeprefix("/entity_artwork/")
        if existing_path.startswith("/entity_artwork/")
        else ""
    )
    if (
        cache is not None
        and persisted_status == "cached"
        and persisted_token == token
        and persisted_identity == identity
        and (not current_token or current_token == token)
    ):
        updated["artwork_cache_status"] = "cached"
        updated["thumbnail"] = f"/entity_artwork/{token}"
        with _ENTITY_RECORDS_LOCK:
            _ENTITY_RECORDS[token] = (server, dict(updated))
        return updated
    # Source URLs and proxy tokens are resolution inputs, never publication
    # proof. Keep them as metadata while the visible thumbnail stays absent.
    updated.pop("thumbnail", None)
    if updated.get("artwork_cache_status") == "cached":
        updated["artwork_cache_status"] = "pending"
    return updated


def attach_persisted_artist_artwork(
    server: Any,
    artist: Dict[str, Any],
) -> Dict[str, Any]:
    """Attach only explicitly persisted verified artwork; never performs R2 I/O."""
    updated = dict(artist or {})
    status = _clean(updated.get("artwork_cache_status")).casefold()
    token = _clean(updated.get("artwork_cache_token"))
    identity = _clean(updated.get("artwork_cache_identity"))
    allowed_tokens = {
        artist_artwork_token(value)
        for value in _artist_cache_identities(updated)
        if artist_artwork_token(value)
    }
    if (
        status == "cached"
        and _TOKEN_RE.match(token)
        and identity
        and token in allowed_tokens
        and token == artist_artwork_token(identity)
    ):
        updated["thumbnail"] = f"/artist_artwork/{token}"
        return updated
    updated.pop("thumbnail", None)
    return updated


def register_entity_metadata_listener(
    listener: Callable[[Dict[str, Any]], None],
) -> None:
    with _ENTITY_UPDATE_LISTENERS_LOCK:
        if listener not in _ENTITY_UPDATE_LISTENERS:
            _ENTITY_UPDATE_LISTENERS.append(listener)


def register_entity_invalidation_listener(
    listener: Callable[[Any, Dict[str, Any]], None],
) -> None:
    with _ENTITY_INVALIDATION_LISTENERS_LOCK:
        if listener not in _ENTITY_INVALIDATION_LISTENERS:
            _ENTITY_INVALIDATION_LISTENERS.append(listener)


def notify_entity_metadata_updated(item: Dict[str, Any]) -> None:
    with _ENTITY_UPDATE_LISTENERS_LOCK:
        listeners = list(_ENTITY_UPDATE_LISTENERS)
    for listener in listeners:
        try:
            listener(dict(item))
        except Exception:
            continue


def schedule_entity_artwork_cache(
    server: Any,
    item: Dict[str, Any],
    *,
    entity_type: str,
    on_cached: Callable[[Dict[str, Any]], None] | None = None,
) -> bool:
    """Resolve bounded sources in background and publish only stored bytes."""
    server = getattr(server, "raw", server)
    normalized_type = _clean(entity_type).casefold()
    cached_item = attach_cached_entity_artwork(
        server,
        item,
        entity_type=normalized_type,
    )
    thumbnail = _clean(cached_item.get("thumbnail"))
    if thumbnail.startswith("/entity_artwork/"):
        notify_entity_metadata_updated(cached_item)
        if on_cached is not None:
            on_cached(cached_item)
        return True

    identity = entity_artwork_identity(
        cached_item,
        entity_type=normalized_type,
    )
    token = artist_artwork_token(identity)
    source_urls = _entity_artwork_source_urls(
        cached_item,
        entity_type=normalized_type,
    )
    cache = get_entity_artwork_cache(server)
    if not identity or not token or not source_urls or cache is None:
        return False
    pending_key = f"{id(server)}:{normalized_type}:{token}"
    with _ENTITY_PENDING_LOCK:
        if pending_key in _ENTITY_PENDING:
            return False
        _ENTITY_PENDING.add(pending_key)

    def run() -> None:
        try:
            selected_source = ""
            failure_times = dict(
                cached_item.get("artwork_source_failures") or {}
                if isinstance(cached_item.get("artwork_source_failures"), dict)
                else {}
            )
            failed_sources = list(
                dict.fromkeys(
                    _clean(value)
                    for value in cached_item.get("artwork_failed_source_urls") or []
                    if _clean(value)
                )
            )
            for source_url in source_urls:
                if cache.store(
                    token=token,
                    source_url=source_url,
                    cache_identity=identity,
                ):
                    selected_source = source_url
                    break
                if source_url not in failed_sources:
                    failed_sources.append(source_url)
                failure_times[source_url] = time.time()
            updated = dict(cached_item)
            updated["artwork_entity_type"] = normalized_type
            updated["artwork_cache_identity"] = identity
            updated["artwork_cache_token"] = token
            updated["artwork_failed_source_urls"] = failed_sources
            updated["artwork_source_failures"] = failure_times
            if selected_source:
                updated["artwork_source_url"] = selected_source
                updated["artwork_source_urls"] = [
                    selected_source,
                    *(url for url in source_urls if url != selected_source),
                ]
                updated["artwork_failed_source_urls"] = [
                    url for url in failed_sources if url != selected_source
                ]
                failure_times.pop(selected_source, None)
                updated["artwork_source_failures"] = failure_times
                updated["artwork_cache_status"] = "cached"
                updated["artwork_cached_at"] = int(time.time())
                updated.pop("artwork_retry_after", None)
                updated["thumbnail"] = f"/entity_artwork/{token}"
                with _ENTITY_RECORDS_LOCK:
                    _ENTITY_RECORDS[token] = (server, dict(updated))
            else:
                updated["artwork_cache_status"] = "source_failed"
                updated["artwork_retry_after"] = time.time() + _SOURCE_RETRY_SECONDS
                updated.pop("thumbnail", None)
            notify_entity_metadata_updated(updated)
            if on_cached is not None:
                on_cached(updated)
        finally:
            with _ENTITY_PENDING_LOCK:
                _ENTITY_PENDING.discard(pending_key)

    _EXECUTOR.submit(run)
    return True


def attach_entity_artwork_proxy(
    server: Any,
    item: Dict[str, Any],
    *,
    entity_type: str,
) -> Dict[str, Any]:
    """Compatibility wrapper; no proxy is attached until bytes exist."""
    updated = attach_cached_entity_artwork(
        server,
        item,
        entity_type=entity_type,
    )
    if not _clean(updated.get("thumbnail")).startswith("/entity_artwork/"):
        schedule_entity_artwork_cache(
            server,
            updated,
            entity_type=entity_type,
        )
    return updated


def read_entity_artwork(
    server: Any,
    token: str,
) -> tuple[bytes, str] | None:
    server = getattr(server, "raw", server)
    cache = get_entity_artwork_cache(server)
    if not _TOKEN_RE.match(_clean(token)):
        return None
    if cache is None:
        return None
    result = cache.read(token)
    if result is not None or not bool(
        getattr(cache, "object_missing", lambda _token: False)(token)
    ):
        return result

    with _ENTITY_RECORDS_LOCK:
        registered = _ENTITY_RECORDS.pop(token, None)
    if registered is None:
        return None
    registered_server, record = registered
    invalidated = dict(record)
    invalidated.pop("thumbnail", None)
    invalidated["artwork_cache_status"] = "missing"
    invalidated["artwork_cache_invalidated_at"] = int(time.time())
    notify_entity_metadata_updated(invalidated)
    with _ENTITY_INVALIDATION_LISTENERS_LOCK:
        listeners = list(_ENTITY_INVALIDATION_LISTENERS)
    for listener in listeners:
        try:
            listener(registered_server, dict(invalidated))
        except Exception:
            continue
    return None


def schedule_artist_artwork_cache(
    server: Any,
    artist: Dict[str, Any],
    *,
    on_cached: Callable[[Dict[str, Any]], None] | None = None,
) -> bool:
    server = getattr(server, "raw", server)
    cached_artist = attach_cached_artist_artwork(server, artist)
    if _clean(cached_artist.get("thumbnail")).startswith("/artist_artwork/"):
        notify_artist_metadata_updated(cached_artist)
        if on_cached is not None:
            on_cached(cached_artist)
        return True

    source_urls = _artist_artwork_source_urls(cached_artist)
    canonical_artist_id = _clean(
        cached_artist.get("canonical_artist_id")
        or cached_artist.get("canonical_artist_key")
    )
    provider_artist_id = _clean(
        cached_artist.get("provider_artist_id")
        or cached_artist.get("browseId")
        or cached_artist.get("artist_id")
        or cached_artist.get("id")
    )
    if (
        provider_artist_id
        and not provider_artist_id.startswith(
            ("musicbrainz:artist:", "artist-name:", "derived:")
        )
        and (
            not canonical_artist_id
            or canonical_artist_id.startswith("artist-name:")
        )
    ):
        canonical_artist_id = f"provider:artist:{provider_artist_id.casefold()}"
    if not canonical_artist_id:
        canonical_artist_id = _clean(
            cached_artist.get("normalized_name") or cached_artist.get("name")
        )
    if (
        not source_urls
        or not canonical_artist_id
    ):
        return False
    cache = get_artist_artwork_cache(server)
    if cache is None:
        return False
    token = artist_artwork_token(canonical_artist_id)
    if not token:
        return False
    pending_key = f"{id(server)}:{token}"
    with _PENDING_LOCK:
        if pending_key in _PENDING:
            return False
        _PENDING.add(pending_key)

    def run() -> None:
        try:
            selected_source = ""
            failure_times = dict(
                cached_artist.get("artwork_source_failures") or {}
                if isinstance(cached_artist.get("artwork_source_failures"), dict)
                else {}
            )
            failed_sources = list(
                dict.fromkeys(
                    _clean(value)
                    for value in cached_artist.get("artwork_failed_source_urls") or []
                    if _clean(value)
                )
            )
            for source_url in source_urls:
                if cache.store(
                    token=token,
                    source_url=source_url,
                    canonical_artist_id=canonical_artist_id,
                ):
                    selected_source = source_url
                    break
                if source_url not in failed_sources:
                    failed_sources.append(source_url)
                failure_times[source_url] = time.time()
            if not selected_source:
                failed = dict(cached_artist)
                failed["artwork_failed_source_urls"] = failed_sources
                failed["artwork_source_failures"] = failure_times
                failed["artwork_cache_status"] = "source_failed"
                notify_artist_metadata_updated(failed)
                if on_cached is not None:
                    on_cached(failed)
                return
            updated = dict(cached_artist)
            updated["artwork_source_url"] = selected_source
            updated["artwork_source_urls"] = [
                selected_source,
                *(url for url in source_urls if url != selected_source),
            ]
            updated["artwork_failed_source_urls"] = [
                url for url in failed_sources if url != selected_source
            ]
            failure_times.pop(selected_source, None)
            updated["artwork_source_failures"] = failure_times
            updated["artwork_cache_status"] = "cached"
            updated["artwork_cached_at"] = int(time.time())
            updated["artwork_cache_identity"] = canonical_artist_id
            updated["artwork_cache_token"] = token
            updated["thumbnail"] = f"/artist_artwork/{token}"
            notify_artist_metadata_updated(updated)
            if on_cached is not None:
                on_cached(updated)
        finally:
            with _PENDING_LOCK:
                _PENDING.discard(pending_key)

    _EXECUTOR.submit(run)
    return True


def read_artist_artwork(server: Any, token: str) -> tuple[bytes, str] | None:
    server = getattr(server, "raw", server)
    cache = get_artist_artwork_cache(server)
    return cache.read(token) if cache is not None else None
