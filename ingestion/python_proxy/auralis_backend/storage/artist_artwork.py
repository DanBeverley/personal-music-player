from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import re
import threading
import time
from typing import Any, Callable, Dict

try:
    import boto3
    from botocore.config import Config
except Exception:  # pragma: no cover - optional production dependency
    boto3 = None
    Config = None


_CLIENTS: Dict[int, "ArtistArtworkCache | None"] = {}
_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="auralis-artist-artwork",
)
_PENDING: set[str] = set()
_PENDING_LOCK = threading.Lock()
_UPDATE_LISTENERS: list[Callable[[Dict[str, Any]], None]] = []
_UPDATE_LISTENERS_LOCK = threading.Lock()
_TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def artist_artwork_token(canonical_artist_id: str) -> str:
    value = _clean(canonical_artist_id).casefold()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32] if value else ""


def artist_artwork_path(canonical_artist_id: str) -> str:
    token = artist_artwork_token(canonical_artist_id)
    return f"/artist_artwork/{token}" if token else ""


def _artist_cache_identities(artist: Dict[str, Any]) -> list[str]:
    identities: list[str] = []

    def add(value: Any) -> None:
        identity = _clean(value)
        if identity and identity.casefold() not in {
            existing.casefold() for existing in identities
        }:
            identities.append(identity)

    add(artist.get("artwork_cache_identity"))
    add(artist.get("canonical_artist_id"))
    add(artist.get("canonical_artist_key"))
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
    normalized_name = re.sub(
        r"[^a-z0-9]+",
        " ",
        _clean(artist.get("normalized_name") or artist.get("name")).casefold(),
    ).strip()
    if normalized_name:
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
    if thumbnail.startswith("/artist_artwork/"):
        return updated
    cache = get_artist_artwork_cache(server)
    if cache is None:
        return updated
    persisted_token = _clean(updated.get("artwork_cache_token"))
    if _TOKEN_RE.match(persisted_token):
        updated["thumbnail"] = f"/artist_artwork/{persisted_token}"
        return updated
    for identity in _artist_cache_identities(updated):
        token = artist_artwork_token(identity)
        if not token or cache.head(token) is None:
            continue
        updated["artwork_cache_identity"] = identity
        updated["artwork_cache_token"] = token
        updated["thumbnail"] = f"/artist_artwork/{token}"
        print(
            "[EBB:artist-artwork] "
            f"status=r2_hit identity={identity} token={token}"
        )
        return updated
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
    def __init__(self, server: Any) -> None:
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
            _clean(getattr(server, "AURALIS_ARTIST_ARTWORK_CACHE_PREFIX", "artist-artwork"))
            or "artist-artwork"
        )
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 2}),
        )

    def _key(self, token: str) -> str:
        return f"{self.prefix.rstrip('/')}/{token}.img"

    def head(self, token: str) -> Dict[str, Any] | None:
        try:
            response = self.client.head_object(
                Bucket=self.bucket,
                Key=self._key(token),
            )
        except Exception:
            return None
        return {
            "content_type": _clean(response.get("ContentType")) or "image/jpeg",
            "content_length": int(response.get("ContentLength") or 0),
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
        except Exception:
            return None
        if not data:
            return None
        return data, _clean(response.get("ContentType")) or "image/jpeg"

    def store(
        self,
        *,
        token: str,
        source_url: str,
        canonical_artist_id: str,
    ) -> bool:
        if not _TOKEN_RE.match(token) or not source_url.startswith(("http://", "https://")):
            return False
        if self.head(token) is not None:
            return True
        try:
            response = self.server.upstream_http.get(
                source_url,
                timeout=(3.0, 8.0),
                stream=True,
            )
            response.raise_for_status()
            content_type = _clean(response.headers.get("Content-Type")).split(";", 1)[0]
            if not content_type.startswith("image/"):
                return False
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > 2 * 1024 * 1024:
                    return False
                chunks.append(chunk)
            data = b"".join(chunks)
            if not data:
                return False
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(token),
                Body=data,
                ContentType=content_type,
                CacheControl="public, max-age=2592000, immutable",
                Metadata={
                    "canonical-artist": canonical_artist_id[:512],
                    "cached-at": str(int(time.time())),
                },
            )
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

    source_url = _clean(
        artist.get("artwork_source_url")
        or artist.get("thumbnail")
    )
    canonical_artist_id = _clean(
        artist.get("canonical_artist_id")
        or artist.get("canonical_artist_key")
    )
    provider_artist_id = _clean(
        artist.get("provider_artist_id")
        or artist.get("browseId")
        or artist.get("artist_id")
        or artist.get("id")
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
            artist.get("normalized_name") or artist.get("name")
        )
    if (
        not source_url.startswith(("http://", "https://"))
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
            if not cache.store(
                token=token,
                source_url=source_url,
                canonical_artist_id=canonical_artist_id,
            ):
                return
            updated = dict(artist)
            updated["artwork_source_url"] = source_url
            updated["artwork_cached_at"] = int(time.time())
            updated["artwork_cache_identity"] = canonical_artist_id
            updated["artwork_cache_token"] = token
            updated["thumbnail"] = f"/artist_artwork/{token}"
            print(
                "[EBB:artist-artwork] "
                f"status=stored identity={canonical_artist_id} token={token}"
            )
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
