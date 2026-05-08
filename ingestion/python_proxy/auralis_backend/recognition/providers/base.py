from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol


class ProviderUnavailableError(RuntimeError):
    pass


class ProviderRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecognitionMatch:
    title: str
    artist: str
    album: str = ""
    confidence: float = 0.0
    duration_ms: int = 0
    provider: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


class RecognitionProvider(Protocol):
    provider_name: str

    def identify_file(self, file_path: str, *, mime_type: str = "") -> List[RecognitionMatch]:
        ...
