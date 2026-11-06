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

_EXPLORATORY_PHRASES = (
    "songs like ",
    "similar to ",
    "music for ",
    "songs for ",
    "song about ",
    "song where ",
    "that song ",
    "sounds like ",
    "reminds me of ",
    "i remember ",
)

_TASTE_INTENT_WORDS = {
    "mix",
    "playlist",
    "songs",
    "music",
    "mood",
    "vibe",
    "workout",
    "focus",
    "sleep",
}


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
    tokens = set(normalized_query.split())
    taste_hits = sum(
        1
        for hint in _SEARCH_TASTE_HINTS
        if hint in normalized_query
    )
    if (
        len(normalized_query) > 38
        or any(normalized_query.startswith(phrase) for phrase in _EXPLORATORY_PHRASES)
        or bool(tokens & _TASTE_INTENT_WORDS)
        or taste_hits >= 2
    ):
        return "taste"
    return "exact"
