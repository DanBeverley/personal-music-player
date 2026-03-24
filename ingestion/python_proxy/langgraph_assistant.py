from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from typing import Any, Dict, List, Optional, TypedDict

import requests

try:
    from ddgs import DDGS
except Exception:  # pragma: no cover - optional dependency
    try:
        from duckduckgo_search import DDGS  # type: ignore
    except Exception:
        DDGS = None

try:
    from langgraph.graph import END, START, StateGraph

    LANGGRAPH_AVAILABLE = True
    LANGGRAPH_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - optional dependency
    END = None
    START = None
    StateGraph = None
    LANGGRAPH_AVAILABLE = False
    LANGGRAPH_IMPORT_ERROR = str(exc)

try:
    from langgraph.checkpoint.memory import MemorySaver

    LANGGRAPH_CHECKPOINT_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    MemorySaver = None
    LANGGRAPH_CHECKPOINT_AVAILABLE = False


_FAST_EVENT_PLAYLIST_CACHE: Dict[str, Dict[str, Any]] = {}
_FAST_EVENT_PLAYLIST_CACHE_TTL_SECONDS = 600
_WEB_SEARCH_CACHE: Dict[str, Dict[str, Any]] = {}
_WEB_SEARCH_CACHE_TTL_SECONDS = 900
_PAGE_HTML_CACHE: Dict[str, Dict[str, Any]] = {}
_PAGE_TEXT_CACHE: Dict[str, Dict[str, Any]] = {}
_PAGE_TEXT_CACHE_TTL_SECONDS = 1800
_EVENT_NOISE_PHRASES = {
    "skip to content",
    "listen",
    "search",
    "menu",
    "home",
    "privacy policy",
    "terms of use",
    "sign in",
    "log in",
    "subscribe",
    "cookie policy",
    "open in app",
    "read more",
    "view all",
    "navigation",
}
_SETLIST_STOP_PHRASES = {
    "tour stats",
    "average setlist",
    "show note",
    "show notes",
    "videos",
    "video",
    "photos",
    "photo",
    "albums",
    "album",
    "tickets",
    "artists covered",
    "cover statistics",
    "cover stats",
    "follow setlist.fm",
    "edit setlist",
}


class AssistantGraphState(TypedDict, total=False):
    session_id: str
    user_scope_id: str
    user_message: str
    conversation_window: List[Dict[str, str]]
    classification: Dict[str, Any]
    memory_hits: List[Dict[str, Any]]
    execution: Dict[str, Any]
    response_payload: Dict[str, Any]
    final_payload: Dict[str, Any]
    diagnostics: Dict[str, Any]
    error: str


_CHECKPOINTER = MemorySaver() if LANGGRAPH_CHECKPOINT_AVAILABLE else None


def langgraph_runtime_available() -> bool:
    return LANGGRAPH_AVAILABLE


def _musicbrainz_headers() -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": "EBBMusicAssistant/2.0 (personal-music-player)",
    }


def _musicbrainz_recording_search(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    response = requests.get(
        "https://musicbrainz.org/ws/2/recording",
        params={"query": query, "fmt": "json", "limit": max(1, min(limit, 8))},
        headers=_musicbrainz_headers(),
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    results = []
    for item in payload.get("recordings", []) or []:
        releases = item.get("releases") or []
        first_release = releases[0] if releases else {}
        results.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "artist": ", ".join(
                    credit.get("name", "")
                    for credit in item.get("artist-credit", []) or []
                    if credit.get("name")
                ),
                "first_release_date": item.get("first-release-date"),
                "release_title": first_release.get("title"),
                "source_url": f"https://musicbrainz.org/recording/{item.get('id')}"
                if item.get("id")
                else None,
            }
        )
    return results


def _musicbrainz_release_search(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    response = requests.get(
        "https://musicbrainz.org/ws/2/release",
        params={"query": query, "fmt": "json", "limit": max(1, min(limit, 8))},
        headers=_musicbrainz_headers(),
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    results = []
    for item in payload.get("releases", []) or []:
        artist_credit = item.get("artist-credit") or []
        results.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "artist": ", ".join(
                    credit.get("name", "")
                    for credit in artist_credit
                    if credit.get("name")
                ),
                "date": item.get("date"),
                "status": item.get("status"),
                "country": item.get("country"),
                "source_url": f"https://musicbrainz.org/release/{item.get('id')}"
                if item.get("id")
                else None,
            }
        )
    return results


def _musicbrainz_artist_search(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    response = requests.get(
        "https://musicbrainz.org/ws/2/artist",
        params={"query": query, "fmt": "json", "limit": max(1, min(limit, 8))},
        headers=_musicbrainz_headers(),
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    results = []
    for item in payload.get("artists", []) or []:
        results.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "sort_name": item.get("sort-name"),
                "country": item.get("country"),
                "type": item.get("type"),
                "disambiguation": item.get("disambiguation"),
                "life_span": item.get("life-span") or {},
                "source_url": f"https://musicbrainz.org/artist/{item.get('id')}"
                if item.get("id")
                else None,
            }
        )
    return results


def _duckduckgo_search(query: str, limit: int = 5, *, force_refresh: bool = False) -> List[Dict[str, Any]]:
    query = (query or "").strip()
    if not query or DDGS is None:
        return []
    cache_key = f"{query.lower()}::{max(1, min(limit, 8))}"
    cached = _WEB_SEARCH_CACHE.get(cache_key)
    now = time.time()
    if (
        not force_refresh
        and cached
        and cached.get("expires_at", 0) > now
    ):
        return list(cached.get("results") or [])
    try:
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=max(1, min(limit, 8)))
            normalized = []
            for item in results or []:
                normalized.append(
                    {
                        "title": item.get("title"),
                        "body": item.get("body"),
                        "href": item.get("href"),
                    }
                )
            _WEB_SEARCH_CACHE[cache_key] = {
                "results": normalized,
                "expires_at": now + _WEB_SEARCH_CACHE_TTL_SECONDS,
            }
            return normalized
    except Exception:
        return []


def _fetch_event_page_html(url: str, *, force_refresh: bool = False) -> str:
    normalized = _trim_text(url)
    if not normalized:
        return ""
    cached = _PAGE_HTML_CACHE.get(normalized)
    now = time.time()
    if (
        not force_refresh
        and cached
        and cached.get("expires_at", 0) > now
    ):
        return cached.get("html") or ""
    try:
        response = requests.get(
            normalized,
            headers={"User-Agent": "EBB/1.0"},
            timeout=(4, 6),
        )
        response.raise_for_status()
        html = response.text
    except Exception:
        return ""
    _PAGE_HTML_CACHE[normalized] = {
        "html": html,
        "expires_at": now + _PAGE_TEXT_CACHE_TTL_SECONDS,
    }
    return html


def _search_web_context(query: str, limit: int = 5, *, force_refresh: bool = False) -> List[Dict[str, Any]]:
    return _duckduckgo_search(query, limit, force_refresh=force_refresh)


def _search_event_performance_facts(query: str, limit: int = 5, *, force_refresh: bool = False) -> List[Dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    normalized = query.lower()
    qualifiers = ["setlist", "songs performed", "songs played"]
    if "site:" not in normalized:
        qualifiers.append("site:setlist.fm")
    return _duckduckgo_search(
        f"{query} {' '.join(qualifiers)}".strip(),
        limit,
        force_refresh=force_refresh,
    )


def _disambiguate_entity(query: str, limit: int = 6) -> List[Dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []

    options: List[Dict[str, Any]] = []
    seen = set()

    def add_option(kind: str, item_id: Optional[str], label: str, detail: Optional[str], value: str):
        key = (kind, item_id or label)
        if key in seen:
            return
        seen.add(key)
        options.append(
            {
                "kind": kind,
                "id": item_id,
                "label": label,
                "detail": detail or "",
                "value": value,
            }
        )

    for item in _musicbrainz_artist_search(query, limit=3):
        detail = item.get("disambiguation") or item.get("country") or item.get("type")
        add_option("artist", item.get("id"), item.get("name") or query, detail, item.get("name") or query)
    for item in _musicbrainz_release_search(query, limit=2):
        detail = " / ".join(filter(None, [item.get("artist"), item.get("date")]))
        add_option("album", item.get("id"), item.get("title") or query, detail, item.get("title") or query)
    for item in _musicbrainz_recording_search(query, limit=2):
        detail = " / ".join(
            filter(None, [item.get("artist"), item.get("first_release_date"), item.get("release_title")])
        )
        add_option("track", item.get("id"), item.get("title") or query, detail, item.get("title") or query)

    return options[: max(1, min(limit, 8))]


def _trim_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _extract_song_titles_from_text(text: str, limit: int = 16) -> List[str]:
    titles: List[str] = []
    seen = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^(?:[-*•]\s+|\d+[.)]\s+)(.+?)\s*$", line)
        if not match:
            continue
        candidate = match.group(1).strip()
        if len(candidate) < 2 or len(candidate) > 90:
            continue
        lowered = candidate.lower()
        if lowered in _EVENT_NOISE_PHRASES:
            continue
        if any(
            phrase in lowered
            for phrase in [
                "their set was",
                "widely regarded",
                "you can find",
                "would you like",
                "the full",
                "performance",
            ]
        ):
            continue
        normalized = _trim_text(candidate).lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        titles.append(candidate)
        if len(titles) >= max(1, min(limit, 20)):
            break
    return titles


def _extract_loose_song_titles_from_text(text: str, limit: int = 16) -> List[str]:
    titles: List[str] = []
    seen = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip().strip("-*•")
        if not line:
            continue
        line = re.sub(r"^\d+[.)]\s*", "", line).strip()
        if len(line) < 2 or len(line) > 90:
            continue
        lowered = line.lower()
        if lowered in _EVENT_NOISE_PHRASES:
            continue
        if any(
            phrase in lowered
            for phrase in [
                "setlist",
                "performed at",
                "wikipedia",
                "youtube",
                "spotify",
                "apple music",
                "official release",
                "concert recording",
                "live aid",
                "wembley",
                "stadium",
                "performance",
                "full set",
            ]
        ):
            continue
        tokens = [token for token in re.split(r"\s+", line) if token]
        if not tokens or len(tokens) > 8:
            continue
        if len(tokens) == 1 and tokens[0].lower() in _EVENT_NOISE_PHRASES:
            continue
        titleish = sum(1 for token in tokens if token[:1].isupper() or token.lower() in {"i", "we", "you"})
        if titleish < max(1, len(tokens) // 2):
            continue
        normalized = _trim_text(line).lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        titles.append(line)
        if len(titles) >= max(1, min(limit, 20)):
            break
    return titles


def _artist_hint_tokens(message: str) -> List[str]:
    stopwords = {
        "live",
        "aid",
        "playlist",
        "songs",
        "song",
        "performed",
        "perform",
        "performance",
        "performances",
        "during",
        "contains",
        "contain",
        "actual",
        "create",
        "build",
        "preferably",
        "version",
        "versions",
        "please",
        "need",
        "want",
        "with",
        "from",
        "into",
        "show",
        "tour",
        "concert",
        "festival",
        "1985",
        "1986",
    }
    hints: List[str] = []
    seen = set()
    for token in re.split(r"[^a-z0-9]+", _trim_text(message).lower()):
        if len(token) < 3 or token in stopwords or token.isdigit():
            continue
        if token in seen:
            continue
        seen.add(token)
        hints.append(token)
        if len(hints) >= 4:
            break
    return hints


def _score_resolved_track(track: Dict[str, Any], title: str, artist_hints: List[str]) -> int:
    normalized_title = _trim_text(title).lower()
    title_tokens = [token for token in re.split(r"[^a-z0-9]+", normalized_title) if token]
    track_title = _trim_text(track.get("title")).lower()
    track_artist = _trim_text(track.get("channel") or track.get("artist")).lower()
    score = 0
    artist_matches = sum(1 for token in artist_hints if token in track_artist)
    if normalized_title and normalized_title == track_title:
        score += 8
    if title_tokens and all(token in track_title for token in title_tokens):
        score += 6
    score += sum(2 for token in title_tokens if token in track_title)
    score += artist_matches * 5
    if artist_hints and artist_matches == 0:
        score -= 8
    if "live" in normalized_title and "live" in track_title:
        score += 2
    return score


def _song_title_variants(title: str) -> List[str]:
    variants: List[str] = []
    seen = set()

    def add(value: str) -> None:
        cleaned = _trim_text(value)
        normalized = cleaned.lower()
        if not cleaned or normalized in seen:
            return
        seen.add(normalized)
        variants.append(cleaned)

    add(title)
    without_parenthetical = re.sub(r"\([^)]*\)", "", title).strip()
    add(without_parenthetical)
    if "/" in without_parenthetical:
        for part in without_parenthetical.split("/"):
            add(part)
    if " - " in without_parenthetical:
        add(without_parenthetical.split(" - ", 1)[0])
    return variants


def _expand_compound_song_titles(title: str) -> List[str]:
    expanded: List[str] = []
    seen = set()

    def add(value: str) -> None:
        cleaned = _trim_text(value)
        normalized = cleaned.lower()
        if not cleaned or normalized in seen:
            return
        seen.add(normalized)
        expanded.append(cleaned)

    cleaned_title = _trim_text(title)
    if not cleaned_title:
        return expanded
    add(cleaned_title)
    if "/" in cleaned_title:
        for part in cleaned_title.split("/"):
            add(part)
    return expanded


def _canonical_song_title(title: str) -> str:
    normalized = _trim_text(title).lower()
    normalized = re.sub(r"\([^)]*\)", " ", normalized)
    normalized = re.sub(r"\[[^\]]*\]", " ", normalized)
    normalized = re.sub(
        r"\b(live|version|mix|edit|remaster(?:ed)?|soundtrack|from|acoustic|demo|take|session|reprise)\b",
        " ",
        normalized,
    )
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _playlist_reply_text(
    req: Any,
    *,
    from_context: bool = False,
    matched_count: int = 0,
) -> str:
    lowered = _trim_text(getattr(req, "message", "")).lower()
    seed = int(hashlib.sha1(f"{lowered}|{matched_count}|{from_context}".encode("utf-8")).hexdigest(), 16)
    if _looks_like_event_question(lowered):
        templates = [
            "I lined up the songs I could match from that performance into a playable draft below.",
            "I pulled that performance into a playable playlist draft below.",
            "I mapped that set into tracks you can play right away below.",
        ]
    elif from_context:
        templates = [
            "I pulled those songs into a playlist draft below.",
            "I shaped those tracks into a playable draft below.",
            "I turned those picks into a playlist you can use below.",
        ]
    else:
        templates = [
            "I put together a playable playlist draft below.",
            "I lined that up as a playlist draft below.",
            "I gathered those tracks into a playable draft below.",
        ]
    return templates[seed % len(templates)]


def _playlist_summary_text(req: Any, *, from_context: bool = False) -> str:
    lowered = _trim_text(getattr(req, "message", "")).lower()
    if _looks_like_event_question(lowered):
        return "Play-ready versions of the songs tied to that performance."
    if from_context:
        return "A playlist draft shaped from the songs already in this conversation."
    return "A playable playlist draft shaped from your request."


def _looks_like_possible_setlist_title(line: str) -> bool:
    cleaned = _trim_text(re.sub(r"^(?:[-*â€¢]\s+|\d+[.)]\s+)", "", line))
    if not cleaned or len(cleaned) > 90:
        return False
    lowered = cleaned.lower()
    if lowered in _EVENT_NOISE_PHRASES:
        return False
    if any(phrase in lowered for phrase in _SETLIST_STOP_PHRASES):
        return False
    if any(
        phrase in lowered
        for phrase in [
            "setlist",
            "wikipedia",
            "youtube",
            "spotify",
            "apple music",
            "wembley",
            "stadium",
            "london",
            "performance",
            "festival",
            "charity",
            "july",
            "note:",
        ]
    ):
        return False
    tokens = [token for token in re.split(r"\s+", cleaned) if token]
    if not tokens or len(tokens) > 10:
        return False
    titleish = sum(1 for token in tokens if token[:1].isupper() or token.lower() in {"i", "we", "you"})
    return titleish >= max(1, len(tokens) // 2)


def _title_match_strength(track: Dict[str, Any], title: str) -> int:
    track_title = _canonical_song_title(track.get("title") or "")
    target_title = _canonical_song_title(title)
    if not track_title or not target_title:
        return 0
    if track_title == target_title:
        return 6
    if track_title.startswith(target_title) or target_title.startswith(track_title):
        return 5
    if target_title in track_title:
        return 4
    target_tokens = [token for token in target_title.split() if token]
    if target_tokens and all(token in track_title for token in target_tokens):
        return 3
    overlap = sum(1 for token in target_tokens if token in track_title)
    return 2 if overlap >= max(1, len(target_tokens) - 1) else 0


def _resolve_song_titles_to_tracks(
    titles: List[str],
    req: Any,
    deps: Dict[str, Any],
    *,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    artist_hints = _artist_hint_tokens(getattr(req, "message", ""))
    artist_hint_text = " ".join(artist_hints[:2]).strip()
    strict_event_match = _looks_like_event_question(getattr(req, "message", ""))
    resolved: List[Dict[str, Any]] = []
    seen_ids = set()
    for title in titles[: max(1, min(limit, 16))]:
        queries = []
        wants_live = "live" in _trim_text(getattr(req, "message", "")).lower()
        for variant in _song_title_variants(title):
            if artist_hint_text and wants_live:
                queries.append(f"{variant} {artist_hint_text} live")
            if artist_hint_text:
                queries.append(f"{variant} {artist_hint_text}")
            if wants_live:
                queries.append(f"{variant} live")
            queries.append(variant)
            if artist_hint_text:
                queries.append(f"{variant} song {artist_hint_text}")
        best_track = None
        best_score = -1
        for query in queries:
            for track in deps["tool_search_tracks"](query, 5):
                track_id = track.get("id")
                if not track_id or track_id in seen_ids:
                    continue
                track_artist = _trim_text(track.get("channel") or track.get("artist")).lower()
                artist_matches = sum(1 for token in artist_hints if token in track_artist)
                title_strength = _title_match_strength(track, title)
                if strict_event_match and artist_hints and artist_matches == 0:
                    continue
                if strict_event_match and title_strength < 3:
                    continue
                score = _score_resolved_track(track, title, artist_hints)
                score += title_strength * 4
                if score > best_score:
                    best_track = track
                    best_score = score
        if best_track is not None and best_track.get("id") not in seen_ids:
            seen_ids.add(best_track["id"])
            resolved.append(best_track)
    return resolved[: max(1, min(limit, 12))]


def _extract_context_song_titles(req: Any, limit: int = 16) -> List[str]:
    titles: List[str] = []
    seen = set()
    for entry in reversed(getattr(req, "conversation", []) or []):
        if getattr(entry, "role", "") != "assistant":
            continue
        for title in _extract_song_titles_from_text(getattr(entry, "content", ""), limit=limit):
            normalized = _trim_text(title).lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            titles.append(title)
            if len(titles) >= max(1, min(limit, 20)):
                return titles
    return titles


def _extract_fact_card_song_titles(
    fact_cards: List[Dict[str, Any]],
    *,
    limit: int = 16,
) -> List[str]:
    titles: List[str] = []
    seen = set()
    for card in fact_cards:
        chunks = [
            _trim_text(card.get("title")),
            _trim_text(card.get("value")),
            _trim_text(card.get("subtitle")),
            *[_trim_text(entry) for entry in (card.get("metadata") or [])],
        ]
        for chunk in chunks:
            if not chunk:
                continue
            for title in _extract_song_titles_from_text(chunk, limit=limit):
                normalized = _trim_text(title).lower()
                if normalized in seen:
                    continue
                seen.add(normalized)
                titles.append(title)
                if len(titles) >= max(1, min(limit, 20)):
                    return titles
    return titles


def _fetch_event_page_text(url: str, *, force_refresh: bool = False) -> str:
    normalized = _trim_text(url)
    if not normalized:
        return ""
    cached = _PAGE_TEXT_CACHE.get(normalized)
    now = time.time()
    if (
        not force_refresh
        and cached
        and cached.get("expires_at", 0) > now
    ):
        return cached.get("text") or ""
    html = _fetch_event_page_html(normalized, force_refresh=force_refresh)
    if not html:
        return ""

    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    html = re.sub(r"(?i)</(li|p|div|br|h1|h2|h3|h4|tr|td|th)>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = text.strip()
    _PAGE_TEXT_CACHE[normalized] = {
        "text": text,
        "expires_at": now + _PAGE_TEXT_CACHE_TTL_SECONDS,
    }
    return text


def _extract_setlist_titles_from_setlistfm_html(html: str, *, limit: int = 16) -> List[str]:
    if not html:
        return []
    titles: List[str] = []
    seen = set()

    def add_title(value: str) -> None:
        cleaned = _trim_text(unescape(re.sub(r"(?s)<[^>]+>", " ", value)))
        for expanded in _expand_compound_song_titles(cleaned):
            normalized = _trim_text(expanded).lower()
            if not normalized or normalized in seen:
                continue
            if not _looks_like_possible_setlist_title(expanded):
                continue
            seen.add(normalized)
            titles.append(expanded)
            if len(titles) >= max(1, min(limit, 24)):
                return

    for match in re.finditer(
        r'<a[^>]+href="[^"]*/song/[^"]*"[^>]*>(.*?)</a>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        add_title(match.group(1))
        if len(titles) >= limit:
            return titles[:limit]

    for match in re.finditer(
        r'<[^>]+class="[^"]*(?:songLabel|songPart|setlistSong)[^"]*"[^>]*>(.*?)</[^>]+>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        add_title(match.group(1))
        if len(titles) >= limit:
            return titles[:limit]

    return titles[:limit]


def _extract_setlist_titles_from_page_text(text: str, *, limit: int = 16) -> List[str]:
    lines = [_trim_text(line) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []

    titles: List[str] = []
    seen = set()
    start_indexes = [
        index
        for index, line in enumerate(lines)
        if "setlist" in line.lower() or "songs played" in line.lower()
    ]
    if not start_indexes:
        start_indexes = [0]

    for start in start_indexes[:2]:
        candidate_lines: List[str] = []
        capturing = False
        for line in lines[start : start + 120]:
            lowered = line.lower()
            if any(phrase in lowered for phrase in _SETLIST_STOP_PHRASES):
                if candidate_lines:
                    break
                continue
            if _looks_like_possible_setlist_title(line):
                capturing = True
                candidate_lines.append(re.sub(r"^(?:[-*â€¢]\s+|\d+[.)]\s+)", "", line).strip())
                continue
            if capturing and len(candidate_lines) >= 3:
                break
        for title in candidate_lines:
            for expanded in _expand_compound_song_titles(title):
                normalized = _trim_text(expanded).lower()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                titles.append(expanded)
                if len(titles) >= max(1, min(limit, 20)):
                    return titles
    return titles


def _extract_song_titles_from_event_results(
    query: str,
    results: List[Dict[str, Any]],
    *,
    limit: int = 16,
    force_refresh: bool = False,
) -> List[str]:
    titles: List[str] = []
    seen = set()

    def add_titles(found: List[str]) -> None:
        for title in found:
            for expanded in _expand_compound_song_titles(title):
                normalized = _trim_text(expanded).lower()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                titles.append(expanded)
                if len(titles) >= max(1, min(limit, 24)):
                    break

    prioritized = sorted(
        results[:6],
        key=lambda item: 0 if "setlist.fm" in _trim_text(item.get("href") or "").lower() else 1,
    )

    setlist_results = [
        item
        for item in prioritized
        if "setlist.fm" in _trim_text(item.get("href") or "").lower()
    ]
    for item in setlist_results[:3]:
        href = item.get("href") or ""
        html = _fetch_event_page_html(href, force_refresh=force_refresh)
        add_titles(_extract_setlist_titles_from_setlistfm_html(html, limit=limit))
        if titles:
            return titles[:limit]
        page_text = _fetch_event_page_text(href, force_refresh=force_refresh)
        if not page_text:
            continue
        add_titles(_extract_setlist_titles_from_page_text(page_text, limit=limit))
        if titles:
            return titles[:limit]

    for item in results:
        chunks = [
            _trim_text(item.get("title")),
            _trim_text(item.get("body")),
        ]
        for chunk in chunks:
            if not chunk:
                continue
            add_titles(_extract_song_titles_from_text(chunk, limit=limit))
            if len(titles) >= limit:
                return titles[:limit]
            add_titles(_extract_loose_song_titles_from_text(chunk, limit=limit))
            if len(titles) >= limit:
                return titles[:limit]

    for item in prioritized:
        if "setlist.fm" in _trim_text(item.get("href") or "").lower():
            continue
        page_text = _fetch_event_page_text(
            item.get("href") or "",
            force_refresh=force_refresh,
        )
        if not page_text:
            continue
        add_titles(_extract_song_titles_from_text(page_text, limit=limit))
        if len(titles) >= limit:
            return titles[:limit]
        add_titles(_extract_loose_song_titles_from_text(page_text, limit=limit))
        if len(titles) >= limit:
            return titles[:limit]

    if not titles and _looks_like_event_question(query):
        add_titles(_extract_loose_song_titles_from_text(query, limit=limit))
    return titles[:limit]


def _suggest_playlist_name(
    req: Any,
    classification: Dict[str, Any],
    response_payload: Dict[str, Any],
    tracks: List[Dict[str, Any]],
) -> str:
    for candidate in [
        _trim_text(response_payload.get("playlist_name")),
        _trim_text(classification.get("playlist_name")),
    ]:
        if candidate and candidate.lower() not in {"ebb mix", "mix", "playlist", "new playlist"}:
            return candidate

    message = _trim_text(getattr(req, "message", ""))
    lowered = message.lower()
    year_match = re.search(r"\b(19|20)\d{2}\b", message)
    year = year_match.group(0) if year_match else ""

    artist_counts: Dict[str, int] = {}
    for track in tracks:
        artist = _trim_text(track.get("channel") or track.get("artist"))
        if not artist:
            continue
        artist_counts[artist] = artist_counts.get(artist, 0) + 1
    primary_artist = ""
    if artist_counts:
        primary_artist = max(
            artist_counts.items(),
            key=lambda item: (item[1], item[0]),
        )[0]

    if "live aid" in lowered:
        if primary_artist:
            return f"{primary_artist}'s Live Aid {year or 'Set'}".strip()
        return f"Live Aid {year}".strip()
    if "top of the pops" in lowered:
        if primary_artist:
            return f"{primary_artist} on Top of the Pops"
        return "Top of the Pops Picks"
    if "comfort" in lowered or "bad day" in lowered or "sad" in lowered:
        if primary_artist:
            return f"{primary_artist} After Hours"
        return "Soft Landing"
    if "surprise" in lowered:
        if primary_artist:
            return f"Unexpected {primary_artist}"
        return "Left-Field Favorites"
    if primary_artist:
        if year:
            return f"{primary_artist} {year}"
        return f"{primary_artist} Essentials"
    if year:
        return f"EBB {year} Mix"
    return "EBB Mix"


def _merge_unique_tracks(*track_lists: List[Dict[str, Any]], limit: int = 12) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen_ids = set()
    for track_list in track_lists:
        for track in track_list or []:
            track_id = _trim_text(track.get("id"))
            if not track_id or track_id in seen_ids:
                continue
            seen_ids.add(track_id)
            merged.append(track)
            if len(merged) >= max(1, min(limit, 20)):
                return merged
    return merged


def _fast_event_playlist_response(req: Any, deps: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not (_looks_like_playlist_create_request(req.message) and _looks_like_event_question(req.message)):
        return None

    cache_key = f"{_trim_text(getattr(req, 'user_scope_id', 'guest'))}::{_trim_text(req.message).lower()}"
    cached = _FAST_EVENT_PLAYLIST_CACHE.get(cache_key)
    now = time.time()
    if not getattr(req, "force_refresh", False) and cached and cached.get("expires_at", 0) > now:
        payload = dict(cached["payload"])
        diagnostics = dict(payload.get("diagnostics") or {})
        diagnostics["cache_hit"] = True
        payload["diagnostics"] = diagnostics
        return payload

    started_at = time.perf_counter()
    fact_results = _search_event_performance_facts(
        req.message,
        limit=6,
        force_refresh=bool(getattr(req, "force_refresh", False)),
    )
    titles = _extract_song_titles_from_event_results(
        req.message,
        fact_results,
        limit=16,
        force_refresh=bool(getattr(req, "force_refresh", False)),
    )
    if not titles:
        return None

    track_matches = _resolve_song_titles_to_tracks(
        titles,
        req,
        deps,
        limit=min(max(len(titles), 6), 12),
    )
    if not track_matches:
        return None

    track_cards = deps["attach_reasons"](track_matches, [])
    playlist_name = _suggest_playlist_name(req, {"mode": "playlist_create"}, {}, track_matches)
    payload = {
        "status": "success",
        "mode": "playlist_create",
        "reply": _playlist_reply_text(req, matched_count=len(track_matches)),
        "follow_up_question": None,
        "tracks": track_cards,
        "playlist_draft": {
            "name": playlist_name,
            "summary": _playlist_summary_text(req),
            "tracks": track_cards,
        },
        "target_playlist": None,
        "playlist_options": [],
        "fact_cards": [],
        "source_links": [],
        "clarification_options": [],
        "action_type": "create_playlist",
        "diagnostics": {
            "mode": "playlist_create",
            "planned_tools": ["fast_event_playlist"],
            "executed_tools": ["search_event_performance_facts", "resolve_song_titles_to_tracks"],
            "timings_ms": {
                "fast_event_playlist": int((time.perf_counter() - started_at) * 1000),
            },
            "total_ms": int((time.perf_counter() - started_at) * 1000),
            "resolved_title_count": len(titles),
            "resolved_track_count": len(track_matches),
            "cache_hit": False,
        },
    }
    deps["store_turn_memory"](
        req,
        {
            "reply": payload["reply"],
            "action_type": payload["action_type"],
            "playlist_name": playlist_name,
            "playlist_summary": payload["playlist_draft"]["summary"],
            "selected_track_ids": [track.get("id") for track in track_matches if track.get("id")],
        },
        selected_tracks=track_matches,
        target_playlist=None,
    )
    _FAST_EVENT_PLAYLIST_CACHE[cache_key] = {
        "payload": payload,
        "expires_at": now + _FAST_EVENT_PLAYLIST_CACHE_TTL_SECONDS,
    }
    return payload


def _fast_context_playlist_response(req: Any, deps: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not _looks_like_playlist_create_request(req.message):
        return None

    started_at = time.perf_counter()
    desired_count = 8
    context_draft_tracks = deps["tool_use_context_tracks"](
        req,
        source="last_playlist_draft_tracks",
        count=desired_count,
    )
    context_assistant_tracks = deps["tool_use_context_tracks"](
        req,
        source="last_assistant_tracks",
        count=desired_count,
    )

    resolved_tracks: List[Dict[str, Any]] = []
    inferred_titles = _extract_context_song_titles(req, limit=16)
    if inferred_titles:
        resolved_tracks = _resolve_song_titles_to_tracks(
            inferred_titles,
            req,
            deps,
            limit=max(desired_count, len(inferred_titles)),
        )

    track_matches = _merge_unique_tracks(
        resolved_tracks,
        context_draft_tracks,
        context_assistant_tracks,
        limit=max(desired_count, 12),
    )
    if not track_matches:
        return None

    track_cards = deps["attach_reasons"](track_matches, [])
    playlist_name = _suggest_playlist_name(req, {"mode": "playlist_create"}, {}, track_matches)
    payload = {
        "status": "success",
        "mode": "playlist_create",
        "reply": _playlist_reply_text(
            req,
            from_context=not _looks_like_event_question(req.message),
            matched_count=len(track_matches),
        ),
        "follow_up_question": None,
        "tracks": track_cards,
        "playlist_draft": {
            "name": playlist_name,
            "summary": _playlist_summary_text(req, from_context=True),
            "tracks": track_cards,
        },
        "target_playlist": None,
        "playlist_options": [],
        "fact_cards": [],
        "source_links": [],
        "clarification_options": [],
        "action_type": "create_playlist",
        "diagnostics": {
            "mode": "playlist_create",
            "planned_tools": ["fast_context_playlist"],
            "executed_tools": [
                "use_context_tracks:last_playlist_draft_tracks",
                "use_context_tracks:last_assistant_tracks",
                *(
                    ["resolve_song_titles_to_tracks"]
                    if resolved_tracks
                    else []
                ),
            ],
            "timings_ms": {
                "fast_context_playlist": int((time.perf_counter() - started_at) * 1000),
            },
            "total_ms": int((time.perf_counter() - started_at) * 1000),
            "resolved_track_count": len(track_matches),
        },
    }
    deps["store_turn_memory"](
        req,
        {
            "reply": payload["reply"],
            "action_type": payload["action_type"],
            "playlist_name": playlist_name,
            "playlist_summary": payload["playlist_draft"]["summary"],
            "selected_track_ids": [track.get("id") for track in track_matches if track.get("id")],
        },
        selected_tracks=track_matches,
        target_playlist=None,
    )
    return payload


def _looks_like_event_question(message: str) -> bool:
    normalized = _trim_text(message).lower()
    return any(
        keyword in normalized
        for keyword in [
            "played at",
            "play at",
            "performed at",
            "perform at",
            "setlist",
            "live aid",
            "top of the pops",
            "concert",
            "festival",
            "show",
            "tour",
            "appearance",
        ]
    )


def _looks_like_release_question(message: str) -> bool:
    normalized = _trim_text(message).lower()
    return any(
        keyword in normalized
        for keyword in [
            "when",
            "what year",
            "released",
            "release date",
            "came out",
            "album",
            "artist facts",
            "who is",
        ]
    )


def _looks_like_simple_conversation(message: str) -> bool:
    normalized = _trim_text(message).lower()
    if not normalized:
        return True
    if normalized in {"hi", "hello", "hey", "yo", "sup", "thanks", "thank you"}:
        return True
    return any(
        phrase in normalized
        for phrase in [
            "can you talk to me",
            "i need someone to talk to",
            "i am having a bad day",
            "i'm having a bad day",
            "i feel bad today",
            "i'm sad",
            "i feel sad",
            "comfort me",
            "talk to me",
        ]
    )


def _looks_like_surprise_music_request(message: str) -> bool:
    normalized = _trim_text(message).lower()
    if not normalized:
        return False
    if normalized in {"surprise me", "surprise me!", "surprise me."}:
        return True
    return normalized.startswith("surprise me")


def _looks_like_playlist_edit_request(message: str) -> bool:
    normalized = _trim_text(message).lower()
    if "playlist" not in normalized:
        return False
    return any(
        phrase in normalized
        for phrase in [
            "add to playlist",
            "remove from playlist",
            "replace in playlist",
            "update playlist",
            "edit playlist",
            "put this in my",
            "add these to my",
        ]
    )


def _looks_like_playlist_create_request(message: str) -> bool:
    normalized = _trim_text(message).lower()
    if "playlist" not in normalized:
        return False
    return any(
        phrase in normalized
        for phrase in [
            "create a playlist",
            "make a playlist",
            "build a playlist",
            "turn this into a playlist",
            "turn those into a playlist",
            "turn these into a playlist",
            "make me a playlist",
            "create for me a playlist",
            "need a playlist",
            "want a playlist",
            "playlist of",
            "playlist for",
            "actual playlist",
            "playlist draft",
        ]
    ) or ("playlist" in normalized and any(
        token in normalized
        for token in [
            "contains",
            "containing",
            "with all",
            "with the songs",
            "consist",
            "consists",
            "consisting",
        ]
    ))


def _looks_like_music_discovery_request(message: str) -> bool:
    normalized = _trim_text(message).lower()
    if not normalized:
        return False
    if _looks_like_surprise_music_request(message):
        return True
    return any(
        phrase in normalized
        for phrase in [
            "find songs like",
            "give me songs",
            "recommend me",
            "recommend some songs",
            "what should i listen to",
            "suggest songs",
            "play something like",
            "similar to",
        ]
    )


def _dedupe_tool_calls(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for raw_call in tool_calls:
        tool = _trim_text(raw_call.get("tool"))
        if not tool:
            continue
        normalized_call = {
            "tool": tool,
            "query": _trim_text(raw_call.get("query")) or None,
            "source": _trim_text(raw_call.get("source")) or None,
            "count": raw_call.get("count"),
            "limit": raw_call.get("limit"),
            "artist_filter": _trim_text(raw_call.get("artist_filter")) or None,
        }
        key = (
            normalized_call["tool"],
            (normalized_call["query"] or "").lower(),
            (normalized_call["source"] or "").lower(),
            normalized_call["count"],
            normalized_call["limit"],
            (normalized_call["artist_filter"] or "").lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized_call)
    return deduped


def _default_deliverable_for_mode(mode: str) -> str:
    normalized = _trim_text(mode).lower()
    if normalized == "music_discovery":
        return "track_suggestions"
    if normalized == "factual_music_qa":
        return "factual_answer"
    if normalized in {"playlist_create", "playlist_edit"}:
        return "playlist_draft"
    if normalized == "playback_action":
        return "playback_action"
    if normalized == "clarify":
        return "clarify"
    return "chat"


def _stabilize_classification(req: Any, classification: Dict[str, Any]) -> Dict[str, Any]:
    message = _trim_text(getattr(req, "message", ""))
    normalized = message.lower()
    stabilized = dict(classification or {})
    mode = _trim_text(stabilized.get("mode")) or "conversation"

    if _looks_like_surprise_music_request(message):
        mode = "music_discovery"
        stabilized["reply"] = None
        stabilized["follow_up_question"] = None
        stabilized["desired_track_count"] = _clamp_count(
            stabilized.get("desired_track_count"),
            low=4,
            high=12,
            default=6,
        )
    elif _looks_like_playlist_edit_request(message):
        mode = "playlist_edit"
        stabilized["reply"] = None
        stabilized["follow_up_question"] = None
    elif _looks_like_playlist_create_request(message):
        mode = "playlist_create"
        stabilized["reply"] = None
        stabilized["follow_up_question"] = None
        stabilized["desired_track_count"] = _clamp_count(
            stabilized.get("desired_track_count"),
            low=4,
            high=12,
            default=8,
        )
    elif mode == "conversation" and (
        _looks_like_event_question(message)
        or _looks_like_release_question(message)
        or _looks_like_music_discovery_request(message)
        or "playlist" in normalized
    ):
        if "playlist" in normalized:
            mode = "playlist_create"
        elif _looks_like_event_question(message) or _looks_like_release_question(message):
            mode = "factual_music_qa"
        else:
            mode = "music_discovery"
        stabilized["reply"] = None
        stabilized["follow_up_question"] = None

    stabilized["mode"] = mode
    stabilized["deliverable"] = (
        _trim_text(stabilized.get("deliverable"))
        or _default_deliverable_for_mode(mode)
    )
    stabilized["tool_calls"] = _dedupe_tool_calls(
        list(stabilized.get("tool_calls") or [])
    )
    return stabilized


def _clamp_count(value: Optional[int], *, low: int = 1, high: int = 12, default: int = 6) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except Exception:
        return default
    return max(low, min(high, parsed))


def _classification_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": [
                    "conversation",
                    "music_discovery",
                    "factual_music_qa",
                    "playlist_create",
                    "playlist_edit",
                    "playback_action",
                    "clarify",
                ],
            },
            "deliverable": {
                "type": "string",
                "enum": [
                    "chat",
                    "track_suggestions",
                    "playlist_draft",
                    "playlist_choice",
                    "factual_answer",
                    "clarify",
                    "playback_action",
                ],
            },
            "reply": {"type": ["string", "null"]},
            "follow_up_question": {"type": ["string", "null"]},
            "playlist_name": {"type": ["string", "null"]},
            "target_playlist_name": {"type": ["string", "null"]},
            "desired_track_count": {"type": ["integer", "null"]},
            "memory_queries": {
                "type": "array",
                "items": {"type": "string"},
            },
            "tool_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {
                            "type": "string",
                            "enum": [
                                "search_tracks",
                                "search_albums",
                                "search_artists",
                                "get_track_details",
                                "get_album_details",
                                "get_similar_tracks",
                                "get_user_taste_profile",
                                "use_context_tracks",
                                "list_playlists",
                                "musicbrainz_recording_search",
                                "musicbrainz_release_search",
                                "musicbrainz_artist_search",
                                "search_web_context",
                                "search_event_performance_facts",
                                "disambiguate_entity",
                            ],
                        },
                        "query": {"type": ["string", "null"]},
                        "source": {
                            "type": ["string", "null"],
                            "enum": [
                                "last_assistant_tracks",
                                "last_playlist_draft_tracks",
                                "recent_assistant_tracks",
                                None,
                            ],
                        },
                        "count": {"type": ["integer", "null"]},
                        "limit": {"type": ["integer", "null"]},
                        "artist_filter": {"type": ["string", "null"]},
                    },
                    "required": ["tool", "query", "source", "count", "limit", "artist_filter"],
                },
            },
        },
        "required": [
            "mode",
            "deliverable",
            "reply",
            "follow_up_question",
            "playlist_name",
            "target_playlist_name",
            "desired_track_count",
            "memory_queries",
            "tool_calls",
        ],
    }


def _response_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            "action_type": {
                "type": "string",
                "enum": [
                    "chat",
                    "suggest_tracks",
                    "create_playlist",
                    "add_to_playlist",
                    "needs_playlist_choice",
                    "clarify",
                    "factual_answer",
                    "playback_action",
                ],
            },
            "follow_up_question": {"type": ["string", "null"]},
            "selected_track_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "reasons": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "reason"],
                },
            },
            "fact_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "clarification_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "playlist_name": {"type": ["string", "null"]},
            "playlist_summary": {"type": ["string", "null"]},
            "target_playlist_name": {"type": ["string", "null"]},
            "playlist_option_names": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "reply",
            "action_type",
            "follow_up_question",
            "selected_track_ids",
            "reasons",
            "fact_ids",
            "clarification_ids",
            "playlist_name",
            "playlist_summary",
            "target_playlist_name",
            "playlist_option_names",
        ],
    }


def _tool_fallback_for_classification(req: Any, classification: Dict[str, Any]) -> List[Dict[str, Any]]:
    mode = classification.get("mode")
    tool_calls = list(classification.get("tool_calls") or [])
    message = _trim_text(getattr(req, "message", ""))
    desired_track_count = _clamp_count(classification.get("desired_track_count"), low=3, high=12, default=6)

    if mode == "factual_music_qa" and not tool_calls:
        if _looks_like_event_question(message):
            tool_calls = [
                {
                    "tool": "search_event_performance_facts",
                    "query": message,
                    "source": None,
                    "count": None,
                    "limit": 5,
                    "artist_filter": None,
                }
            ]
        elif _looks_like_release_question(message):
            tool_calls = [
                {
                    "tool": "musicbrainz_recording_search",
                    "query": message,
                    "source": None,
                    "count": None,
                    "limit": 4,
                    "artist_filter": None,
                },
                {
                    "tool": "musicbrainz_release_search",
                    "query": message,
                    "source": None,
                    "count": None,
                    "limit": 3,
                    "artist_filter": None,
                },
            ]
        else:
            tool_calls = [
                {
                    "tool": "musicbrainz_artist_search",
                    "query": message,
                    "source": None,
                    "count": None,
                    "limit": 3,
                    "artist_filter": None,
                },
                {
                    "tool": "search_web_context",
                    "query": message,
                    "source": None,
                    "count": None,
                    "limit": 4,
                    "artist_filter": None,
                },
            ]
    elif mode in {"playlist_create", "playlist_edit"} and not tool_calls:
        if _looks_like_event_question(message):
            tool_calls = [
                {
                    "tool": "search_event_performance_facts",
                    "query": message,
                    "source": None,
                    "count": None,
                    "limit": 5,
                    "artist_filter": None,
                },
                {
                    "tool": "search_tracks",
                    "query": message,
                    "source": None,
                    "count": None,
                    "limit": desired_track_count,
                    "artist_filter": None,
                },
            ]
        else:
            tool_calls = [
                {
                    "tool": "use_context_tracks",
                    "query": None,
                    "source": "last_assistant_tracks",
                    "count": desired_track_count,
                    "limit": desired_track_count,
                    "artist_filter": None,
                },
                {
                    "tool": "search_tracks",
                    "query": message,
                    "source": None,
                    "count": None,
                    "limit": desired_track_count,
                    "artist_filter": None,
                },
            ]
    elif mode in {"music_discovery", "playback_action"} and not tool_calls:
        tool_calls = [
            {
                "tool": "get_user_taste_profile",
                "query": message,
                "source": None,
                "count": None,
                "limit": 1,
                "artist_filter": None,
            },
            {
                "tool": "search_tracks",
                "query": message,
                "source": None,
                "count": desired_track_count,
                "limit": desired_track_count,
                "artist_filter": None,
            }
        ]
        if "album" in message.lower():
            tool_calls.append(
                {
                    "tool": "search_albums",
                    "query": message,
                    "source": None,
                    "count": None,
                    "limit": 4,
                    "artist_filter": None,
                }
            )
        if "artist" in message.lower() or "band" in message.lower():
            tool_calls.append(
                {
                    "tool": "search_artists",
                    "query": message,
                    "source": None,
                    "count": None,
                    "limit": 4,
                    "artist_filter": None,
                }
            )
    return tool_calls


def _recording_fact_card(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    title = _trim_text(item.get("title")) or f"Recording {index + 1}"
    release_date = _trim_text(item.get("first_release_date"))
    release_title = _trim_text(item.get("release_title"))
    artist = _trim_text(item.get("artist"))
    metadata = [entry for entry in [artist and f"Artist: {artist}", release_title and f"Release: {release_title}"] if entry]
    value = release_date or "Release date unavailable"
    subtitle = "Recording information"
    return {
        "id": f"recording:{item.get('id') or index}",
        "kind": "release_info",
        "title": title,
        "value": value,
        "subtitle": subtitle,
        "metadata": metadata,
        "source_label": "MusicBrainz",
        "source_url": item.get("source_url"),
    }


def _release_fact_card(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    title = _trim_text(item.get("title")) or f"Release {index + 1}"
    value = _trim_text(item.get("date")) or "Release date unavailable"
    subtitle = "Release information"
    metadata = [
        entry
        for entry in [
            item.get("artist") and f"Artist: {item.get('artist')}",
            item.get("status") and f"Status: {item.get('status')}",
            item.get("country") and f"Country: {item.get('country')}",
        ]
        if entry
    ]
    return {
        "id": f"release:{item.get('id') or index}",
        "kind": "album_info",
        "title": title,
        "value": value,
        "subtitle": subtitle,
        "metadata": metadata,
        "source_label": "MusicBrainz",
        "source_url": item.get("source_url"),
    }


def _artist_fact_card(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    name = _trim_text(item.get("name")) or f"Artist {index + 1}"
    life_span = item.get("life_span") or {}
    begin = _trim_text(life_span.get("begin"))
    ended = _trim_text(life_span.get("ended"))
    country = _trim_text(item.get("country"))
    artist_type = _trim_text(item.get("type"))
    value_parts = [part for part in [artist_type, country] if part]
    value = " / ".join(value_parts) or "Artist information"
    metadata = [
        entry
        for entry in [
            item.get("disambiguation") and f"Disambiguation: {item.get('disambiguation')}",
            begin and f"Started: {begin}",
            ended and f"Ended: {ended}",
        ]
        if entry
    ]
    return {
        "id": f"artist:{item.get('id') or index}",
        "kind": "artist_fact",
        "title": name,
        "value": value,
        "subtitle": "Artist information",
        "metadata": metadata,
        "source_label": "MusicBrainz",
        "source_url": item.get("source_url"),
    }


def _web_fact_card(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    return {
        "id": f"web:{index}",
        "kind": "web_result",
        "title": _trim_text(item.get("title")) or f"Web result {index + 1}",
        "value": _trim_text(item.get("body")) or "Open the source for details.",
        "subtitle": "Web result",
        "metadata": [],
        "source_label": "DuckDuckGo",
        "source_url": item.get("href"),
    }


def _album_search_fact_card(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    title = _trim_text(item.get("title")) or f"Album {index + 1}"
    artist = _trim_text(item.get("artist"))
    year = _trim_text(item.get("year"))
    return {
        "id": f"album-search:{item.get('id') or index}",
        "kind": "album_info",
        "title": title,
        "value": year or "Album match",
        "subtitle": artist or "Album search",
        "metadata": [entry for entry in [artist and f"Artist: {artist}", year and f"Year: {year}"] if entry],
        "source_label": "YouTube Music",
        "source_url": None,
    }


def _artist_search_fact_card(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    name = _trim_text(item.get("name")) or f"Artist {index + 1}"
    description = _trim_text(item.get("description"))
    return {
        "id": f"artist-search:{item.get('id') or index}",
        "kind": "artist_fact",
        "title": name,
        "value": description or "Artist match",
        "subtitle": "Artist search",
        "metadata": [],
        "source_label": "YouTube Music",
        "source_url": None,
    }


def _track_details_fact_cards(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    title = _trim_text(item.get("title")) or "Track details"
    artist = _trim_text(item.get("author"))
    release_date = _trim_text(item.get("release_date"))
    album = _trim_text(item.get("album") or item.get("album_title"))
    metadata = [entry for entry in [artist and f"Artist: {artist}", album and f"Album: {album}"] if entry]
    return [
        {
            "id": f"track-details:{item.get('video_id') or title}",
            "kind": "track_info",
            "title": title,
            "value": release_date or "Track details",
            "subtitle": "Track information",
            "metadata": metadata,
            "source_label": "YouTube Music",
            "source_url": None,
        }
    ]


def _album_details_fact_cards(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    title = _trim_text(item.get("title")) or "Album details"
    artist = _trim_text(item.get("artist"))
    year = _trim_text(item.get("year"))
    return [
        {
            "id": f"album-details:{item.get('id') or title}",
            "kind": "album_info",
            "title": title,
            "value": year or "Album details",
            "subtitle": artist or "Album information",
            "metadata": [
                entry
                for entry in [
                    artist and f"Artist: {artist}",
                    item.get("track_count") and f"Tracks: {item.get('track_count')}",
                ]
                if entry
            ],
            "source_label": "YouTube Music",
            "source_url": None,
        }
    ]


def _clarification_option_from_item(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    detail = _trim_text(item.get("detail"))
    return {
        "id": f"clarify:{item.get('kind')}:{item.get('id') or index}",
        "kind": item.get("kind") or "entity",
        "label": _trim_text(item.get("label")) or f"Option {index + 1}",
        "value": _trim_text(item.get("value")) or _trim_text(item.get("label")) or "",
        "description": detail,
    }


def _collect_source_links(fact_cards: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    seen = set()
    links: List[Dict[str, str]] = []
    for card in fact_cards:
        url = _trim_text(card.get("source_url"))
        if not url or url in seen:
            continue
        seen.add(url)
        label = _trim_text(card.get("source_label")) or "Source"
        links.append({"label": label, "url": url})
    return links


def _with_stage_timing(
    state: AssistantGraphState,
    stage: str,
    started_at: float,
    **extras: Any,
) -> Dict[str, Any]:
    diagnostics = dict(state.get("diagnostics") or {})
    timings = dict(diagnostics.get("timings_ms") or {})
    timings[stage] = int((time.perf_counter() - started_at) * 1000)
    diagnostics["timings_ms"] = timings
    for key, value in extras.items():
        diagnostics[key] = value
    return diagnostics


def run_langgraph_assistant(req: Any, deps: Dict[str, Any]) -> Dict[str, Any]:
    if not LANGGRAPH_AVAILABLE:
        raise RuntimeError(f"LangGraph is not available: {LANGGRAPH_IMPORT_ERROR}")
    total_started_at = time.perf_counter()
    fast_event_payload = _fast_event_playlist_response(req, deps)
    if fast_event_payload is not None:
        return fast_event_payload
    fast_context_payload = _fast_context_playlist_response(req, deps)
    if fast_context_payload is not None:
        return fast_context_payload
    if _looks_like_simple_conversation(getattr(req, "message", "")):
        reply = deps["fallback_chat_reply"](req)
        total_ms = int((time.perf_counter() - total_started_at) * 1000)
        return {
            "status": "success",
            "mode": "conversation",
            "reply": reply,
            "follow_up_question": None,
            "tracks": [],
            "playlist_draft": None,
            "target_playlist": None,
            "playlist_options": [],
            "fact_cards": [],
            "source_links": [],
            "clarification_options": [],
            "action_type": "chat",
            "diagnostics": {
                "timings_ms": {"fast_conversation": total_ms},
                "mode": "conversation",
                "planned_tools": [],
                "executed_tools": [],
                "total_ms": total_ms,
            },
        }

    def classify_intent(state: AssistantGraphState) -> AssistantGraphState:
        started_at = time.perf_counter()
        prompt = {
            "user_message": req.message,
            "conversation": [
                {"role": entry.role, "content": entry.content}
                for entry in req.conversation[-8:]
            ],
            "available_playlists": deps["tool_list_playlists"](req),
            "last_assistant_tracks": deps["all_context_tracks"](req)["last_assistant_tracks"][:8],
            "last_playlist_draft_tracks": deps["all_context_tracks"](req)["last_playlist_draft_tracks"][:8],
            "recent_assistant_tracks": deps["all_context_tracks"](req)["recent_assistant_tracks"][:10],
            "recent_queries": list(getattr(req, "recent_queries", []) or [])[:8],
            "recent_track_ids": list(getattr(req, "recent_track_ids", []) or [])[:8],
            "library_tracks": [
                {
                    "id": track.id,
                    "title": track.title,
                    "artist": track.artist,
                    "album": track.album,
                }
                for track in (getattr(req, "library_tracks", []) or [])[:12]
            ],
            "tools": {
                "search_tracks": "Find playable tracks in the current catalog.",
                "search_albums": "Find relevant albums in the catalog.",
                "search_artists": "Find relevant artists in the catalog.",
                "get_track_details": "Fetch metadata and similar songs for a specific track when you already have a concrete track id.",
                "get_album_details": "Fetch album details and album tracks when you already have a concrete album id.",
                "get_similar_tracks": "Fetch similar tracks for a known track id.",
                "get_user_taste_profile": "Fetch a lightweight taste profile from recent assistant and user activity.",
                "use_context_tracks": "Reuse songs already suggested or drafted in this conversation.",
                "list_playlists": "Inspect the user's existing playlists.",
                "musicbrainz_recording_search": "Lookup song metadata and release dates.",
                "musicbrainz_release_search": "Lookup album and release metadata.",
                "musicbrainz_artist_search": "Lookup artist facts and disambiguation metadata.",
                "search_web_context": "Search the web for broad factual music context when structured sources are not enough.",
                "search_event_performance_facts": "Search for setlists, performances, appearances, and event-specific music facts.",
                "disambiguate_entity": "Generate clarification choices when the request is ambiguous.",
            },
        }
        messages = [
            {
                "role": "system",
                "content": (
                "You are EBB, a professional music assistant with strong conversation skills. "
                    "First decide the mode and the deliverable. "
                    "If the user wants normal conversation or comfort, stay conversational and provide a real reply. "
                    "If the user asks factual music questions, use factual tools. "
                    "If they want discovery, playlist creation, playlist editing, or playback actions, choose tools accordingly. "
                    "If the user refers to prior suggestions, prefer use_context_tracks. "
                    "For greetings and pure conversation, do not call tools unless they are truly needed. "
                    "Return only JSON matching the schema."
                ),
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ]
        try:
            classification = deps.get("call_planner_structured", deps["call_structured"])(
                messages,
                schema=_classification_schema(),
                temperature=0.25,
                timeout_seconds=60,
            )
            classification_fallback = None
        except Exception as exc:
            lowered = _trim_text(req.message).lower()
            if "playlist" in lowered:
                fallback_mode = "playlist_edit" if any(
                    phrase in lowered for phrase in ["add to", "replace", "remove from", "update playlist"]
                ) else "playlist_create"
            elif _looks_like_event_question(req.message) or _looks_like_release_question(req.message) or "?" in lowered:
                fallback_mode = "factual_music_qa"
            else:
                fallback_mode = "music_discovery"
            classification = {
                "mode": fallback_mode,
                "deliverable": _default_deliverable_for_mode(fallback_mode),
                "reply": None,
                "follow_up_question": None,
                "playlist_name": None,
                "target_playlist_name": None,
                "desired_track_count": 6,
                "memory_queries": [],
                "tool_calls": [],
            }
            classification_fallback = str(exc)
        classification["tool_calls"] = _tool_fallback_for_classification(req, classification)
        classification = _stabilize_classification(req, classification)
        if not classification.get("tool_calls"):
            classification["tool_calls"] = _dedupe_tool_calls(
                _tool_fallback_for_classification(req, classification)
            )
        diagnostics = _with_stage_timing(
            state,
            "classify_intent",
            started_at,
            mode=classification.get("mode") or "conversation",
            deliverable=classification.get("deliverable") or _default_deliverable_for_mode(classification.get("mode") or "conversation"),
            planned_tools=[
                raw_call.get("tool")
                for raw_call in (classification.get("tool_calls") or [])
                if raw_call.get("tool")
            ],
            classification_fallback=classification_fallback,
        )
        return {"classification": classification, "diagnostics": diagnostics}

    def route_after_classification(state: AssistantGraphState) -> str:
        classification = state.get("classification") or {}
        if classification.get("tool_calls"):
            return "retrieve_memory"
        if classification.get("mode") in {
            "music_discovery",
            "factual_music_qa",
            "playlist_create",
            "playlist_edit",
            "playback_action",
        }:
            return "retrieve_memory"
        return "finalize_direct_response"

    def retrieve_memory(state: AssistantGraphState) -> AssistantGraphState:
        started_at = time.perf_counter()
        classification = state.get("classification") or {}
        queries = [req.message]
        queries.extend(list(classification.get("memory_queries") or []))
        memory_hits = deps["query_memory"](
            req.user_scope_id,
            queries,
            limit=8,
        )
        diagnostics = _with_stage_timing(
            state,
            "retrieve_memory",
            started_at,
            memory_hit_count=len(memory_hits),
        )
        return {"memory_hits": memory_hits, "diagnostics": diagnostics}

    def execute_tools(state: AssistantGraphState) -> AssistantGraphState:
        started_at = time.perf_counter()
        classification = state.get("classification") or {}
        tool_calls = list(classification.get("tool_calls") or [])
        desired_track_count = _clamp_count(
            classification.get("desired_track_count"),
            low=3,
            high=12,
            default=6,
        )
        track_pool: List[Dict[str, Any]] = []
        track_map: Dict[str, Dict[str, Any]] = {}
        fact_cards: List[Dict[str, Any]] = []
        clarification_options: List[Dict[str, Any]] = []
        tool_outputs: List[Dict[str, Any]] = []

        def add_tracks(source: str, items: List[Dict[str, Any]]):
            normalized_items = []
            for item in items:
                track_id = item.get("id")
                if not track_id:
                    continue
                normalized_items.append(item)
                if track_id not in track_map:
                    track_map[track_id] = item
                    track_pool.append(item)
            tool_outputs.append({"tool": source, "tracks": normalized_items})

        def run_single_tool(raw_call: Dict[str, Any]) -> Dict[str, Any]:
            tool = raw_call.get("tool")
            query = raw_call.get("query") or req.message
            limit = _clamp_count(raw_call.get("limit"), low=1, high=12, default=5)
            if tool == "search_tracks":
                return {
                    "tool": f"search_tracks:{query}",
                    "tracks": deps["tool_search_tracks"](query, max(1, min(limit, 12))),
                }
            if tool == "search_albums":
                return {
                    "tool": f"search_albums:{query}",
                    "results": deps["tool_search_albums"](query, max(1, min(limit, 8))),
                }
            if tool == "search_artists":
                return {
                    "tool": f"search_artists:{query}",
                    "results": deps["tool_search_artists"](query, max(1, min(limit, 8))),
                }
            if tool == "get_track_details":
                track_details = deps["tool_get_track_details"](query)
                return {
                    "tool": f"get_track_details:{query}",
                    "track_details": track_details,
                    "tracks": list(track_details.get("similar_tracks") or [])[: max(1, min(limit, 8))],
                }
            if tool == "get_album_details":
                album_details = deps["tool_get_album_details"](query)
                return {
                    "tool": f"get_album_details:{query}",
                    "album_details": album_details,
                    "tracks": list(album_details.get("tracks") or [])[: max(1, min(limit, 10))],
                }
            if tool == "get_similar_tracks":
                return {
                    "tool": f"get_similar_tracks:{query}",
                    "tracks": deps["tool_get_similar_tracks"](query, max(1, min(limit, 10))),
                }
            if tool == "get_user_taste_profile":
                return {
                    "tool": "get_user_taste_profile",
                    "profile": deps["tool_get_user_taste_profile"](req),
                }
            if tool == "use_context_tracks":
                source = raw_call.get("source") or "last_assistant_tracks"
                count = _clamp_count(raw_call.get("count"), low=1, high=12, default=5)
                return {
                    "tool": f"use_context_tracks:{source}",
                    "tracks": deps["tool_use_context_tracks"](
                        req,
                        source=source,
                        count=count,
                        artist_filter=raw_call.get("artist_filter"),
                    ),
                }
            if tool == "list_playlists":
                return {"tool": "list_playlists", "playlists": deps["tool_list_playlists"](req)}
            if tool == "musicbrainz_recording_search":
                return {
                    "tool": f"musicbrainz_recording_search:{query}",
                    "results": _musicbrainz_recording_search(query, min(limit, 5)),
                }
            if tool == "musicbrainz_release_search":
                return {
                    "tool": f"musicbrainz_release_search:{query}",
                    "results": _musicbrainz_release_search(query, min(limit, 5)),
                }
            if tool == "musicbrainz_artist_search":
                return {
                    "tool": f"musicbrainz_artist_search:{query}",
                    "results": _musicbrainz_artist_search(query, min(limit, 5)),
                }
            if tool == "search_web_context":
                return {
                    "tool": f"search_web_context:{query}",
                    "results": _search_web_context(
                        query,
                        min(limit, 5),
                        force_refresh=bool(getattr(req, "force_refresh", False)),
                    ),
                }
            if tool == "search_event_performance_facts":
                return {
                    "tool": f"search_event_performance_facts:{query}",
                    "results": _search_event_performance_facts(
                        query,
                        min(limit, 5),
                        force_refresh=bool(getattr(req, "force_refresh", False)),
                    ),
                }
            if tool == "disambiguate_entity":
                return {
                    "tool": f"disambiguate_entity:{query}",
                    "options": _disambiguate_entity(query, min(limit, 6)),
                }
            return {"tool": str(tool or "unknown"), "results": []}

        if len(tool_calls) <= 1:
            tool_results = [run_single_tool(raw_call) for raw_call in tool_calls]
        else:
            tool_results = []
            with ThreadPoolExecutor(max_workers=min(len(tool_calls), 4)) as executor:
                futures = [executor.submit(run_single_tool, raw_call) for raw_call in tool_calls]
                for future in as_completed(futures):
                    try:
                        tool_results.append(future.result())
                    except Exception as exc:
                        tool_outputs.append({"tool": "error", "error": str(exc)})

        for result in tool_results:
            tool_name = result.get("tool") or "unknown"
            if result.get("tracks"):
                add_tracks(tool_name, result.get("tracks") or [])
                continue
            if result.get("playlists") is not None:
                tool_outputs.append(result)
                continue
            if result.get("profile") is not None:
                tool_outputs.append(result)
                continue
            if result.get("options"):
                options = result.get("options") or []
                clarification_options.extend(
                    _clarification_option_from_item(item, index)
                    for index, item in enumerate(options)
                )
                tool_outputs.append(result)
                continue
            if result.get("results"):
                raw_results = result.get("results") or []
                if tool_name.startswith("musicbrainz_recording_search:"):
                    fact_cards.extend(
                        _recording_fact_card(item, index)
                        for index, item in enumerate(raw_results)
                    )
                elif tool_name.startswith("musicbrainz_release_search:"):
                    fact_cards.extend(
                        _release_fact_card(item, index)
                        for index, item in enumerate(raw_results)
                    )
                elif tool_name.startswith("musicbrainz_artist_search:"):
                    fact_cards.extend(
                        _artist_fact_card(item, index)
                        for index, item in enumerate(raw_results)
                    )
                elif tool_name.startswith("search_albums:"):
                    fact_cards.extend(
                        _album_search_fact_card(item, index)
                        for index, item in enumerate(raw_results)
                    )
                elif tool_name.startswith("search_artists:"):
                    fact_cards.extend(
                        _artist_search_fact_card(item, index)
                        for index, item in enumerate(raw_results)
                    )
                elif tool_name.startswith("search_web_context:") or tool_name.startswith("search_event_performance_facts:"):
                    fact_cards.extend(
                        _web_fact_card(item, index)
                        for index, item in enumerate(raw_results)
                    )
                tool_outputs.append(result)
                continue
            if result.get("track_details"):
                fact_cards.extend(_track_details_fact_cards(result.get("track_details") or {}))
                tool_outputs.append(result)
                continue
            if result.get("album_details"):
                fact_cards.extend(_album_details_fact_cards(result.get("album_details") or {}))
                tool_outputs.append(result)

        if (
            classification.get("mode") in {"playlist_create", "playlist_edit", "playback_action"}
            and not track_pool
            and _looks_like_event_question(req.message)
        ):
            fallback_tracks = deps["tool_search_tracks"](
                req.message,
                max(4, min(desired_track_count, 10)),
            )
            if fallback_tracks:
                add_tracks("fallback_event_track_search", fallback_tracks)
        if classification.get("mode") in {"playlist_create", "playlist_edit"}:
            inferred_titles = _extract_fact_card_song_titles(
                fact_cards,
                limit=max(6, desired_track_count),
            )
            if inferred_titles:
                inferred_tracks = _resolve_song_titles_to_tracks(
                    inferred_titles,
                    req,
                    deps,
                    limit=max(4, desired_track_count),
                )
                if inferred_tracks:
                    if _looks_like_event_question(req.message):
                        track_pool.clear()
                        track_map.clear()
                    add_tracks("resolved_fact_card_song_titles", inferred_tracks)
        if classification.get("mode") in {"playlist_create", "playlist_edit"} and not track_pool:
            inferred_titles = _extract_context_song_titles(req, limit=max(6, desired_track_count))
            if inferred_titles:
                inferred_tracks = _resolve_song_titles_to_tracks(
                    inferred_titles,
                    req,
                    deps,
                    limit=max(4, desired_track_count),
                )
                if inferred_tracks:
                    add_tracks("resolved_context_song_titles", inferred_tracks)

        diagnostics = _with_stage_timing(
            state,
            "execute_tools",
            started_at,
            executed_tools=[result.get("tool") for result in tool_results if result.get("tool")],
            tool_error_count=sum(
                1 for output in tool_outputs if output.get("tool") == "error"
            ),
            fact_card_count=len(fact_cards[:10]),
            track_pool_count=len(track_pool),
        )
        return {
            "execution": {
                "tool_outputs": tool_outputs,
                "track_pool": track_pool,
                "track_map": track_map,
                "fact_cards": fact_cards[:10],
                "clarification_options": clarification_options[:8],
            },
            "diagnostics": diagnostics,
        }

    def synthesize_response(state: AssistantGraphState) -> AssistantGraphState:
        started_at = time.perf_counter()
        classification = state.get("classification") or {}
        execution = state.get("execution") or {
            "tool_outputs": [],
            "track_pool": [],
            "track_map": {},
            "fact_cards": [],
            "clarification_options": [],
        }
        prompt = {
            "user_message": req.message,
            "conversation": [
                {"role": entry.role, "content": entry.content}
                for entry in req.conversation[-10:]
            ],
            "mode": classification.get("mode"),
            "deliverable": classification.get("deliverable"),
            "memory_hits": state.get("memory_hits", []),
            "tool_outputs": execution.get("tool_outputs", []),
            "available_tracks": execution.get("track_pool", [])[:20],
            "available_track_ids": [
                track.get("id")
                for track in execution.get("track_pool", [])
                if track.get("id")
            ],
            "fact_cards": execution.get("fact_cards", [])[:8],
            "clarification_options": execution.get("clarification_options", [])[:6],
            "available_playlists": deps["tool_list_playlists"](req),
            "classification_reply": classification.get("reply"),
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You are EBB, a warm, capable music assistant. "
                    "Answer naturally and directly. "
                    "Satisfy the requested deliverable exactly. "
                    "If the user asked a factual question, answer the question first, then optionally mention that more detail is below. "
                    "If the user wants songs, recommend only from available_track_ids and vary the number of picks naturally. "
                    "If the user wants a playlist, use the available tracks or context tracks instead of inventing unrelated songs. "
                    "When the user asks for a playlist and available_track_ids exists, return action_type=create_playlist and include selected_track_ids. "
                    "Choose a specific playlist_name shaped by the conversation or the energy of the songs, never a generic name like EBB Mix. "
                    "If there are clarification options, ask a clean follow-up only when ambiguity still matters. "
                    "Never say you cannot access music streaming services, playlists, or music tools. "
                    "If the request is actionable and tracks are available, act like a capable in-app assistant and use them. "
                    "If tracks are not available yet, say you are narrowing it down and ask one helpful follow-up. "
                    "Avoid canned lead-ins. "
                    "Return only JSON matching the schema."
                ),
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ]
        response_payload = deps.get("call_response_structured", deps["call_structured"])(
            messages,
            schema=_response_schema(),
            temperature=0.45,
        )
        diagnostics = _with_stage_timing(
            state,
            "synthesize_response",
            started_at,
            action_type=response_payload.get("action_type") or "chat",
        )
        return {"response_payload": response_payload, "diagnostics": diagnostics}

    def finalize_direct_response(state: AssistantGraphState) -> AssistantGraphState:
        started_at = time.perf_counter()
        classification = state.get("classification") or {}
        reply = _trim_text(classification.get("reply")) or deps["fallback_chat_reply"](req)
        requested_deliverable = (
            _trim_text(classification.get("deliverable"))
            or _default_deliverable_for_mode(classification.get("mode") or "conversation")
        )
        direct_action_type = "chat"
        if requested_deliverable == "clarify" or classification.get("mode") == "clarify":
            direct_action_type = "clarify"
        elif requested_deliverable == "factual_answer":
            direct_action_type = "factual_answer"
        final_payload = {
            "status": "success",
            "mode": classification.get("mode") or "conversation",
            "reply": reply,
            "follow_up_question": classification.get("follow_up_question"),
            "tracks": [],
            "playlist_draft": None,
            "target_playlist": None,
            "playlist_options": [],
            "fact_cards": [],
            "source_links": [],
            "clarification_options": [],
            "action_type": direct_action_type,
            "diagnostics": {},
        }
        diagnostics = _with_stage_timing(
            state,
            "finalize_direct_response",
            started_at,
            total_ms=int((time.perf_counter() - total_started_at) * 1000),
        )
        final_payload["diagnostics"] = diagnostics
        return {
            "response_payload": {
                "reply": reply,
                "action_type": final_payload["action_type"],
                "follow_up_question": final_payload["follow_up_question"],
                "selected_track_ids": [],
                "reasons": [],
                "fact_ids": [],
                "clarification_ids": [],
                "playlist_name": classification.get("playlist_name"),
                "playlist_summary": None,
                "target_playlist_name": classification.get("target_playlist_name"),
                "playlist_option_names": [],
            },
            "final_payload": final_payload,
            "diagnostics": diagnostics,
        }

    def finalize_payload(state: AssistantGraphState) -> AssistantGraphState:
        started_at = time.perf_counter()
        if state.get("final_payload"):
            return {}

        classification = state.get("classification") or {}
        response_payload = state.get("response_payload") or {}
        execution = state.get("execution") or {
            "track_pool": [],
            "track_map": {},
            "fact_cards": [],
            "clarification_options": [],
        }

        selected_tracks: List[Dict[str, Any]] = []
        seen_track_ids = set()
        for track_id in response_payload.get("selected_track_ids") or []:
            if not track_id or track_id in seen_track_ids:
                continue
            seen_track_ids.add(track_id)
            track = execution.get("track_map", {}).get(track_id)
            if track is not None:
                selected_tracks.append(track)

        if not selected_tracks and classification.get("mode") in {"music_discovery", "playlist_create", "playlist_edit", "playback_action"}:
            desired = _clamp_count(classification.get("desired_track_count"), low=3, high=12, default=6)
            selected_tracks = list(execution.get("track_pool", [])[:desired])

        track_cards = deps["attach_reasons"](
            selected_tracks,
            response_payload.get("reasons") or [],
        )

        requested_deliverable = (
            _trim_text(classification.get("deliverable"))
            or _default_deliverable_for_mode(classification.get("mode") or "conversation")
        )
        action_type = (response_payload.get("action_type") or "chat").strip() or "chat"
        target_playlist_name = _trim_text(
            response_payload.get("target_playlist_name")
            or classification.get("target_playlist_name")
        )
        target_playlist = None
        if target_playlist_name:
            target_playlist = deps["playlist_lookup_by_name"](
                req.playlist_summaries,
                target_playlist_name,
            )

        playlist_draft = None
        playlist_options: List[Dict[str, Any]] = []
        if action_type == "needs_playlist_choice":
            playlist_options = deps["playlist_options"](
                req,
                response_payload.get("playlist_option_names") or [],
            )
        elif action_type == "add_to_playlist" and target_playlist is None:
            action_type = "needs_playlist_choice"
            playlist_options = deps["playlist_options"](req)
        elif action_type == "create_playlist" and track_cards:
            playlist_draft = {
                "name": _suggest_playlist_name(
                    req,
                    classification,
                    response_payload,
                    selected_tracks,
                ),
                "summary": _trim_text(response_payload.get("playlist_summary")) or "A playlist shaped from your conversation.",
                "tracks": track_cards,
            }

        if requested_deliverable == "playlist_draft" and track_cards:
            action_type = "create_playlist"
            playlist_draft = {
                "name": _suggest_playlist_name(
                    req,
                    classification,
                    response_payload,
                    selected_tracks,
                ),
                "summary": _trim_text(response_payload.get("playlist_summary")) or "A playlist shaped from your conversation.",
                "tracks": track_cards,
            }
        elif requested_deliverable == "track_suggestions" and track_cards and action_type == "chat":
            action_type = "suggest_tracks"
        elif requested_deliverable == "factual_answer" and action_type == "chat":
            action_type = "factual_answer"

        if (
            classification.get("mode") == "playlist_create"
            and track_cards
            and action_type not in {"create_playlist", "add_to_playlist", "needs_playlist_choice"}
        ):
            action_type = "create_playlist"
            playlist_draft = {
                "name": _suggest_playlist_name(
                    req,
                    classification,
                    response_payload,
                    selected_tracks,
                ),
                "summary": _trim_text(response_payload.get("playlist_summary")) or "A playlist shaped from your conversation.",
                "tracks": track_cards,
            }

        fact_card_map = {card["id"]: card for card in execution.get("fact_cards", [])}
        selected_fact_cards: List[Dict[str, Any]] = []
        for fact_id in response_payload.get("fact_ids") or []:
            card = fact_card_map.get(fact_id)
            if card is not None:
                selected_fact_cards.append(card)
        if not selected_fact_cards:
            selected_fact_cards = list(execution.get("fact_cards", [])[:6])

        clarification_map = {
            option["id"]: option for option in execution.get("clarification_options", [])
        }
        selected_clarifications: List[Dict[str, Any]] = []
        for option_id in response_payload.get("clarification_ids") or []:
            option = clarification_map.get(option_id)
            if option is not None:
                selected_clarifications.append(option)
        if not selected_clarifications and action_type == "clarify":
            selected_clarifications = list(execution.get("clarification_options", [])[:6])

        if classification.get("mode") == "factual_music_qa" and not selected_fact_cards:
            action_type = "clarify" if selected_clarifications else "chat"

        if action_type in {"suggest_tracks", "create_playlist", "add_to_playlist", "playback_action"} and not track_cards:
            action_type = "clarify"

        if action_type == "clarify" and not selected_clarifications:
            selected_clarifications = list(execution.get("clarification_options", [])[:6])

        if action_type == "clarify" and not response_payload.get("follow_up_question") and selected_clarifications:
            response_payload["follow_up_question"] = "I found a few close matches. Which one did you mean?"

        target_playlist_payload = None
        if target_playlist is not None:
            target_playlist_payload = {
                "id": target_playlist.id,
                "name": target_playlist.name,
                "track_count": target_playlist.track_count,
            }

        reply = _trim_text(response_payload.get("reply"))
        if not reply:
            if action_type == "create_playlist" and playlist_draft:
                reply = _playlist_reply_text(
                    req,
                    from_context=not _looks_like_event_question(req.message),
                    matched_count=len(track_cards),
                )
            elif action_type == "suggest_tracks" and track_cards:
                reply = "I pulled together a set of playable tracks below."
            elif classification.get("mode") == "factual_music_qa" and selected_fact_cards:
                reply = selected_fact_cards[0].get("value") or selected_fact_cards[0].get("title") or deps["fallback_chat_reply"](req)
            elif classification.get("reply"):
                reply = _trim_text(classification.get("reply"))
            else:
                reply = deps["fallback_chat_reply"](req)

        if classification.get("mode") in {"playlist_create", "playlist_edit"} and not track_cards:
            inferred_titles = _extract_song_titles_from_text(reply, limit=16)
            if not inferred_titles:
                inferred_titles = _extract_fact_card_song_titles(
                    execution.get("fact_cards", []),
                    limit=16,
                )
            if not inferred_titles:
                inferred_titles = _extract_context_song_titles(req, limit=16)
            if inferred_titles:
                inferred_tracks = _resolve_song_titles_to_tracks(
                    inferred_titles,
                    req,
                    deps,
                    limit=_clamp_count(classification.get("desired_track_count"), low=3, high=12, default=6),
                )
                if inferred_tracks:
                    track_cards = deps["attach_reasons"](
                        inferred_tracks,
                        response_payload.get("reasons") or [],
                    )
                    action_type = "create_playlist"
                    playlist_draft = {
                        "name": _suggest_playlist_name(
                            req,
                            classification,
                            response_payload,
                            inferred_tracks,
                        ),
                        "summary": _trim_text(response_payload.get("playlist_summary")) or _playlist_summary_text(req, from_context=True),
                        "tracks": track_cards,
                    }

        if action_type == "create_playlist" and track_cards:
            selected_fact_cards = []
            if _looks_like_event_question(req.message):
                reply = _playlist_reply_text(req, matched_count=len(track_cards))

        final_payload = {
            "status": "success",
            "mode": classification.get("mode") or "conversation",
            "reply": reply,
            "follow_up_question": response_payload.get("follow_up_question"),
            "tracks": track_cards,
            "playlist_draft": playlist_draft,
            "target_playlist": target_playlist_payload,
            "playlist_options": playlist_options,
            "fact_cards": selected_fact_cards,
            "source_links": _collect_source_links(selected_fact_cards),
            "clarification_options": selected_clarifications,
            "action_type": action_type,
            "diagnostics": {},
        }
        diagnostics = _with_stage_timing(
            state,
            "finalize_payload",
            started_at,
            total_ms=int((time.perf_counter() - total_started_at) * 1000),
        )
        final_payload["diagnostics"] = diagnostics
        return {"final_payload": final_payload, "diagnostics": diagnostics}

    def persist_memory(state: AssistantGraphState) -> AssistantGraphState:
        started_at = time.perf_counter()
        final_payload = state.get("final_payload") or {}
        execution = state.get("execution") or {"track_map": {}}
        response_payload = state.get("response_payload") or {}
        selected_tracks: List[Dict[str, Any]] = []
        seen_ids = set()
        for track_id in response_payload.get("selected_track_ids") or []:
            if not track_id or track_id in seen_ids:
                continue
            seen_ids.add(track_id)
            track = execution.get("track_map", {}).get(track_id)
            if track is not None:
                selected_tracks.append(track)
        deps["store_turn_memory"](
            req,
            {
                **response_payload,
                "reply": final_payload.get("reply"),
                "action_type": final_payload.get("action_type"),
                "playlist_name": (final_payload.get("playlist_draft") or {}).get("name"),
                "playlist_summary": (final_payload.get("playlist_draft") or {}).get("summary"),
            },
            selected_tracks=selected_tracks,
            target_playlist=final_payload.get("target_playlist"),
        )
        diagnostics = _with_stage_timing(
            state,
            "persist_memory",
            started_at,
            total_ms=int((time.perf_counter() - total_started_at) * 1000),
        )
        final_payload["diagnostics"] = diagnostics
        return {"diagnostics": diagnostics, "final_payload": final_payload}

    graph = StateGraph(AssistantGraphState)
    graph.add_node("classify_intent", classify_intent)
    graph.add_node("retrieve_memory", retrieve_memory)
    graph.add_node("execute_tools", execute_tools)
    graph.add_node("synthesize_response", synthesize_response)
    graph.add_node("finalize_direct_response", finalize_direct_response)
    graph.add_node("finalize_payload", finalize_payload)
    graph.add_node("persist_memory", persist_memory)
    graph.add_edge(START, "classify_intent")
    graph.add_conditional_edges(
        "classify_intent",
        route_after_classification,
        {
            "retrieve_memory": "retrieve_memory",
            "finalize_direct_response": "finalize_direct_response",
        },
    )
    graph.add_edge("retrieve_memory", "execute_tools")
    graph.add_edge("execute_tools", "synthesize_response")
    graph.add_edge("synthesize_response", "finalize_payload")
    graph.add_edge("finalize_direct_response", "persist_memory")
    graph.add_edge("finalize_payload", "persist_memory")
    graph.add_edge("persist_memory", END)

    compiled = graph.compile(checkpointer=_CHECKPOINTER) if _CHECKPOINTER is not None else graph.compile()
    session_id = _trim_text(getattr(req, "session_id", None)) or "transient"
    initial_state: AssistantGraphState = {
        "session_id": session_id,
        "user_scope_id": _trim_text(getattr(req, "user_scope_id", "guest")) or "guest",
        "user_message": req.message,
        "conversation_window": [
            {"role": entry.role, "content": entry.content}
            for entry in req.conversation[-10:]
        ],
        "classification": {},
        "memory_hits": [],
        "execution": {},
        "response_payload": {},
        "final_payload": {},
        "error": "",
        "diagnostics": {
            "timings_ms": {},
            "mode": None,
            "planned_tools": [],
            "executed_tools": [],
        },
    }

    if _CHECKPOINTER is not None:
        result = compiled.invoke(
            initial_state,
            config={
                "configurable": {
                    "thread_id": (
                        f"ebb-assistant::{getattr(req, 'user_scope_id', 'guest')}::{session_id}"
                    )
                }
            },
        )
    else:
        result = compiled.invoke(initial_state)
    return result.get("final_payload") or {
        "status": "success",
        "mode": "conversation",
        "reply": deps["fallback_chat_reply"](req),
        "follow_up_question": None,
        "tracks": [],
        "playlist_draft": None,
        "target_playlist": None,
        "playlist_options": [],
        "fact_cards": [],
        "source_links": [],
        "clarification_options": [],
        "action_type": "chat",
    }
