from __future__ import annotations

from typing import Callable


_SEARCH_TASTE_HINTS = (
    "mix",
    "playlist",
    "songs",
    "music",
    "mood",
    "vibe",
    "rock",
    "metal",
    "jazz",
    "blues",
    "classical",
    "punk",
    "indie",
    "folk",
    "country",
    "ambient",
    "chill",
    "sleep",
    "focus",
    "lofi",
    "edm",
    "house",
    "techno",
    "trance",
    "pop",
    "rap",
    "hip hop",
)


def resolve_search_mode(
    query: str,
    *,
    normalize_text_fn: Callable[[str], str],
    intent_hint: str = "",
    explicit_mode: str = "",
) -> str:
    normalized_explicit = explicit_mode.strip().lower()
    if normalized_explicit == "taste":
        return "taste"
    if normalized_explicit == "entity":
        return "entity"
    if normalized_explicit == "exact" and intent_hint not in {"artist", "album"}:
        return normalized_explicit
    if intent_hint in {"artist", "album"}:
        return "entity"
    normalized_query = normalize_text_fn(query)
    if not normalized_query:
        return "exact"
    if len(normalized_query) > 38 or any(
        hint in normalized_query for hint in _SEARCH_TASTE_HINTS
    ):
        return "taste"
    return "exact"
