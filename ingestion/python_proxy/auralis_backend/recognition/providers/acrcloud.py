from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Any, Dict, List

import requests

from .base import (
    ProviderRequestError,
    ProviderUnavailableError,
    RecognitionMatch,
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return 0


class ACRCloudRecognitionProvider:
    provider_name = "acrcloud"

    def __init__(
        self,
        *,
        host: str = "",
        access_key: str = "",
        access_secret: str = "",
        timeout_seconds: float = 25.0,
    ) -> None:
        self._host = _clean_text(host or os.environ.get("AURALIS_ACRCLOUD_HOST"))
        self._access_key = _clean_text(
            access_key or os.environ.get("AURALIS_ACRCLOUD_ACCESS_KEY")
        )
        self._access_secret = _clean_text(
            access_secret or os.environ.get("AURALIS_ACRCLOUD_ACCESS_SECRET")
        )
        self._timeout_seconds = max(5.0, float(timeout_seconds))

    @property
    def available(self) -> bool:
        return bool(self._host and self._access_key and self._access_secret)

    def _require_available(self) -> None:
        if self.available:
            return
        raise ProviderUnavailableError(
            "ACRCloud credentials are not configured. Expected "
            "AURALIS_ACRCLOUD_HOST, AURALIS_ACRCLOUD_ACCESS_KEY, and "
            "AURALIS_ACRCLOUD_ACCESS_SECRET."
        )

    def _identify_url(self) -> str:
        host = self._host
        if host.startswith("http://") or host.startswith("https://"):
            return f"{host.rstrip('/')}/v1/identify"
        return f"https://{host}/v1/identify"

    def identify_file(self, file_path: str, *, mime_type: str = "") -> List[RecognitionMatch]:
        self._require_available()
        http_method = "POST"
        http_uri = "/v1/identify"
        data_type = "audio"
        signature_version = "1"
        timestamp = str(int(time.time()))
        string_to_sign = "\n".join(
            [
                http_method,
                http_uri,
                self._access_key,
                data_type,
                signature_version,
                timestamp,
            ]
        )
        signature = base64.b64encode(
            hmac.new(
                self._access_secret.encode("ascii"),
                string_to_sign.encode("ascii"),
                digestmod=hashlib.sha1,
            ).digest()
        ).decode("ascii")

        sample_bytes = os.path.getsize(file_path)
        data = {
            "access_key": self._access_key,
            "sample_bytes": str(sample_bytes),
            "timestamp": timestamp,
            "signature": signature,
            "data_type": data_type,
            "signature_version": signature_version,
        }
        with open(file_path, "rb") as handle:
            files = {
                "sample": (
                    os.path.basename(file_path),
                    handle,
                    mime_type or "application/octet-stream",
                )
            }
            try:
                response = requests.post(
                    self._identify_url(),
                    data=data,
                    files=files,
                    timeout=(5.0, self._timeout_seconds),
                )
            except requests.RequestException as exc:
                raise ProviderRequestError(f"ACRCloud request failed: {exc}") from exc

        if response.status_code >= 400:
            raise ProviderRequestError(
                f"ACRCloud returned HTTP {response.status_code}: {response.text[:220]}"
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise ProviderRequestError("ACRCloud returned a non-JSON response") from exc
        return self._extract_matches(payload)

    def _extract_matches(self, payload: Dict[str, Any]) -> List[RecognitionMatch]:
        status = payload.get("status") or {}
        status_code = _as_int(status.get("code"))
        if status_code not in (0,):
            return []
        metadata = payload.get("metadata") or {}
        music = metadata.get("music") or []
        matches: List[RecognitionMatch] = []
        for entry in music:
            if not isinstance(entry, dict):
                continue
            title = _clean_text(entry.get("title"))
            artists = entry.get("artists") or []
            artist_name = ""
            if isinstance(artists, list):
                for artist in artists:
                    if isinstance(artist, dict):
                        artist_name = _clean_text(artist.get("name"))
                    else:
                        artist_name = _clean_text(artist)
                    if artist_name:
                        break
            album = entry.get("album") or {}
            album_name = (
                _clean_text(album.get("name"))
                if isinstance(album, dict)
                else _clean_text(album)
            )
            if not title or not artist_name:
                continue
            matches.append(
                RecognitionMatch(
                    title=title,
                    artist=artist_name,
                    album=album_name,
                    confidence=float(entry.get("score") or 0.0),
                    duration_ms=_as_int(entry.get("duration_ms")),
                    provider=self.provider_name,
                    raw=dict(entry),
                )
            )
        return matches
