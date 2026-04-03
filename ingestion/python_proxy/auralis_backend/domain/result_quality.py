from __future__ import annotations

from typing import Any, Dict, Iterable


_HARD_REJECT_TERMS = (
    "tribute",
    "karaoke",
    "cover",
    "covers",
    "nightcore",
    "8d audio",
    "chipmunk",
)

_SOFT_PENALTY_TERMS = (
    "instrumental",
    "live",
    "remix",
    "remaster",
    "remastered",
    "radio edit",
    "sped up",
    "slowed",
)


def _normalize(server, value: Any) -> str:
    return server._normalize_text(value or "")


def _combined_text(server, values: Iterable[Any]) -> str:
    return " ".join(
        normalized
        for normalized in (_normalize(server, value) for value in values)
        if normalized
    )


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    return any(term in text for term in terms)


def track_result_penalty(
    server,
    track: Dict[str, Any],
    *,
    query: str = "",
    normalized_anchor_artists: Iterable[str] = (),
) -> float:
    normalized_title = _normalize(server, track.get("title"))
    normalized_artist = _normalize(
        server,
        track.get("channel") or track.get("artist") or track.get("author"),
    )
    normalized_album = _normalize(server, track.get("album"))
    normalized_query = _normalize(server, query)
    combined = _combined_text(
        server,
        [normalized_title, normalized_artist, normalized_album],
    )
    if not combined:
        return 0.0

    penalty = 0.0
    query_allows_derivative = _contains_any(normalized_query, _HARD_REJECT_TERMS)
    hard_hit = _contains_any(combined, _HARD_REJECT_TERMS)
    if hard_hit and not query_allows_derivative:
        penalty += 2.8
    if not query_allows_derivative and _contains_any(normalized_album, ("tribute", "karaoke")):
        penalty += 1.9
    if not query_allows_derivative and _contains_any(normalized_artist, ("tribute", "karaoke", "cover")):
        penalty += 1.4
    if _contains_any(normalized_title, _SOFT_PENALTY_TERMS) and not _contains_any(
        normalized_query,
        _SOFT_PENALTY_TERMS,
    ):
        penalty += 0.55

    normalized_anchor_artist_set = {
        value for value in normalized_anchor_artists if isinstance(value, str) and value
    }
    if hard_hit and not query_allows_derivative and normalized_anchor_artist_set and normalized_artist:
        if normalized_artist not in normalized_anchor_artist_set:
            penalty += 0.9

    return penalty


def artist_result_penalty(server, artist: Dict[str, Any], *, query: str = "") -> float:
    combined = _combined_text(
        server,
        [artist.get("name"), artist.get("description")],
    )
    if not combined:
        return 0.0
    penalty = 0.0
    query_allows_derivative = _contains_any(_normalize(server, query), _HARD_REJECT_TERMS)
    if not query_allows_derivative and _contains_any(combined, _HARD_REJECT_TERMS):
        penalty += 2.6
    if not query_allows_derivative and _contains_any(combined, ("tribute act", "cover band", "karaoke")):
        penalty += 1.4
    return penalty


def album_result_penalty(server, album: Dict[str, Any], *, query: str = "") -> float:
    combined = _combined_text(
        server,
        [album.get("title"), album.get("artist")],
    )
    if not combined:
        return 0.0
    penalty = 0.0
    query_allows_derivative = _contains_any(_normalize(server, query), _HARD_REJECT_TERMS)
    if not query_allows_derivative and _contains_any(combined, _HARD_REJECT_TERMS):
        penalty += 2.4
    if not query_allows_derivative and _contains_any(combined, ("tribute", "karaoke")):
        penalty += 1.2
    return penalty
