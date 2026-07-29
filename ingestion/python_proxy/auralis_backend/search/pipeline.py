from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List

from ..domain.result_quality import (
    album_result_penalty,
    artist_result_penalty,
    track_result_penalty,
)
from ..domain.catalog import (
    normalize_artist_name,
    normalize_track_title,
    normalized_popularity,
    verified_playback_source,
)
from .server_adapter import adapt_search_server
from .canonical import source_quality_score
from .intelligence import search_text_similarity
from .runtime import semantic_search_lexical_score


def _finalize_ranked_tracks(
    server: Any,
    ranked: List[Dict[str, Any]],
    *,
    limit: int,
    query_intent: str = "",
) -> List[Dict[str, Any]]:
    server = adapt_search_server(server)
    ranked.sort(
        key=lambda item: (
            float(item.get("score") or 0.0),
            item.get("title") or "",
        ),
        reverse=True,
    )
    results: List[Dict[str, Any]] = []
    artist_counts: Dict[str, int] = {}
    title_counts: Dict[str, int] = {}
    for track in ranked:
        artist_key = server.normalize_text(
            track.get("channel") or track.get("artist") or ""
        )
        intent = str(query_intent or "").strip().lower()
        if artist_key and intent not in {"artist", "album"}:
            artist_count = artist_counts.get(artist_key, 0)
            max_artist_results = (
                max(6, int(limit * 0.60))
                if intent == "track"
                else max(3, int(limit * 0.35))
            )
            if artist_count >= max_artist_results and len(results) + 1 < limit:
                continue
        title_key = server.normalize_text(track.get("title") or "")
        if title_key:
            title_count = title_counts.get(title_key, 0)
            if title_count >= 2 and len(results) + 1 < limit:
                continue
        if artist_key:
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        if title_key:
            title_counts[title_key] = title_counts.get(title_key, 0) + 1
        results.append(track)
        if len(results) >= limit:
            break
    return results


def _track_relevance(
    server: Any,
    *,
    query: str,
    track: Dict[str, Any],
    query_intent: str,
    source_names: List[str],
) -> Dict[str, float]:
    playable = bool(verified_playback_source(track))
    title = server.trim_text(track.get("title") or track.get("name"))
    artist = server.trim_text(track.get("channel") or track.get("artist"))
    album = server.trim_text(track.get("album") or track.get("album_title"))
    title_score = search_text_similarity(query, title)
    artist_score = search_text_similarity(query, artist)
    album_score = search_text_similarity(query, album)
    normalized_query_title = normalize_track_title(query)
    normalized_result_title = normalize_track_title(title)
    query_tokens = {
        token for token in normalized_query_title.split() if len(token) >= 4
    } or set(normalized_query_title.split())
    title_tokens = set(normalized_result_title.split())
    normalized_result_artist = normalize_artist_name(artist)
    artist_tokens = set(normalized_result_artist.split())
    full_query_tokens = set(normalized_query_title.split())
    artist_mentioned = bool(
        artist_tokens
        and artist_tokens.issubset(full_query_tokens)
    )
    title_token_overlap = (
        len(query_tokens & title_tokens) / len(query_tokens)
        if query_tokens
        else 0.0
    )
    title_contains_query = bool(
        normalized_query_title
        and normalized_result_title
        and (
            normalized_query_title in normalized_result_title
            or normalized_result_title in normalized_query_title
        )
    )
    combined_score = max(
        search_text_similarity(query, f"{title} {artist}".strip()) * 0.96,
        search_text_similarity(query, f"{artist} {title}".strip()) * 0.96,
    )
    intent = str(query_intent or "").strip().lower()
    relationship_sources = set(source_names or [])
    relationship_match = False
    if intent == "artist":
        relationship_match = bool(
            relationship_sources & {"artist_catalog", "same_artist_catalog"}
        )
        admitted = (
            artist_score >= 0.70
            or combined_score >= 0.78
            or relationship_match
        )
    elif intent == "album":
        relationship_match = "album_tracklist" in relationship_sources
        admitted = (
            album_score >= 0.70
            or combined_score >= 0.80
            or relationship_match
        )
    else:
        relationship_match = bool(
            relationship_sources
            & {
                "same_artist_catalog",
                "artist_catalog",
                "album_tracklist",
            }
        )
        provider_hint = "provider_intent" in relationship_sources
        provider_supported = provider_hint and (
            artist_mentioned
            or title_token_overlap >= 0.5
            or title_contains_query
        )
        lexical_match = (
            title_score >= 0.70
            and (
                title_token_overlap >= 0.5
                or title_contains_query
            )
        )
        admitted = (
            lexical_match
            or (
                combined_score >= 0.82
                and title_token_overlap >= 0.5
            )
            or relationship_match
            or provider_supported
        )
    if intent in {"artist", "album"}:
        provider_hint = "provider_intent" in relationship_sources
    return {
        "title": title_score,
        "artist": artist_score,
        "album": album_score,
        "combined": combined_score,
        "relationship": 1.0 if relationship_match else 0.0,
        "provider_hint": 1.0 if provider_hint else 0.0,
        "title_token_overlap": title_token_overlap,
        "artist_mentioned": 1.0 if artist_mentioned else 0.0,
        "admitted": 1.0 if admitted and playable else 0.0,
    }


def rank_track_candidates_fast_path(
    server: Any,
    req,
    retrieval_payload: Dict[str, Any],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    server = adapt_search_server(server)
    query = server.trim_text(req.query)
    track_candidates = retrieval_payload.get("track_candidates") or {}
    if not track_candidates:
        return []
    normalized_query = server.normalize_text(query)
    normalized_canonical_query = normalize_track_title(query)
    normalized_anchor_artists = retrieval_payload.get("normalized_anchor_artists") or set()
    canonical_resolution = dict(
        retrieval_payload.get("canonical_resolution") or {}
    )
    resolved_artist = dict(retrieval_payload.get("resolved_artist") or {})
    resolved_artist_name = server.trim_text(
        resolved_artist.get("name")
        or resolved_artist.get("artist")
        or resolved_artist.get("channel")
    )
    canonical_title = server.trim_text(canonical_resolution.get("title"))
    canonical_artist = server.trim_text(canonical_resolution.get("artist"))
    exact_title_artist_counts: Dict[str, int] = {}
    for entry in track_candidates.values():
        payload = (entry or {}).get("payload")
        if not isinstance(payload, dict):
            continue
        if normalize_track_title(payload.get("title") or "") != normalized_canonical_query:
            continue
        artist_key = server.normalize_text(
            payload.get("channel") or payload.get("artist") or ""
        )
        if artist_key:
            exact_title_artist_counts[artist_key] = (
                exact_title_artist_counts.get(artist_key, 0) + 1
            )
    ranked: List[Dict[str, Any]] = []
    for entry in track_candidates.values():
        payload = (entry or {}).get("payload")
        if not isinstance(payload, dict):
            continue
        track = server.normalize_track(payload)
        if track is None:
            continue
        source_scores = dict((entry or {}).get("source_scores") or {})
        source_names = sorted(
            source_name
            for source_name in source_scores.keys()
            if server.trim_text(source_name)
        )
        relevance = _track_relevance(
            server,
            query=query,
            track=track,
            query_intent=str(retrieval_payload.get("query_intent") or ""),
            source_names=source_names,
        )
        if (
            str(retrieval_payload.get("query_intent") or "") == "artist"
            and resolved_artist_name
            and search_text_similarity(
                resolved_artist_name,
                track.get("channel") or track.get("artist") or "",
            )
            < 0.86
            and not (
                set(source_names)
                & {"artist_catalog", "same_artist_catalog"}
            )
        ):
            continue
        if relevance["admitted"] <= 0.0:
            continue
        source_score = max(source_scores.values(), default=0.0)
        retrieval_votes = max(len(source_names), 1)
        title = track.get("title")
        artist = track.get("channel") or track.get("artist")
        album = track.get("album")
        lexical_score = semantic_search_lexical_score(query, title, artist, album, server=server)
        title_lexical = semantic_search_lexical_score(query, title, server=server)
        normalized_title = server.normalize_text(title or "")
        exact_title_match = 1.0 if normalized_title and normalized_title == normalized_query else 0.0
        anchor_artist_match = (
            1.0
            if server.normalize_text(artist or "") in normalized_anchor_artists
            else 0.0
        )
        authority_bonus = source_quality_score(server, track)
        popularity_bonus = normalized_popularity(track)
        canonical_title_match = (
            search_text_similarity(canonical_title, title)
            if canonical_title
            else 0.0
        )
        canonical_artist_match = (
            search_text_similarity(canonical_artist, artist)
            if canonical_artist
            else 0.0
        )
        canonical_pair_match = (
            canonical_title_match * 0.56 + canonical_artist_match * 0.44
            if canonical_title and canonical_artist
            else 0.0
        )
        ambiguity_penalty = 0.0
        if exact_title_match and len(exact_title_artist_counts) >= 3:
            if authority_bonus < 0.8 and not anchor_artist_match:
                ambiguity_penalty = 1.25
        canonical_mismatch_penalty = 0.0
        if canonical_title_match >= 0.90 and canonical_artist_match < 0.62:
            canonical_mismatch_penalty = 4.5
        ranking_score = (
            (float(source_score) * 1.2)
            + (float(retrieval_votes) * 0.45)
            + (float(lexical_score) * 6.0)
            + (float(title_lexical) * 5.4)
            + (float(exact_title_match) * 2.4)
            + (float(anchor_artist_match) * 0.65)
            + (float(authority_bonus) * 1.15)
            + (float(popularity_bonus) * 0.85)
            + (float(canonical_pair_match) * 6.0)
            + (float(relevance["combined"]) * 2.0)
            + (
                float(relevance["relationship"])
                * (
                    1.4
                    if "provider_intent" in source_names
                    else 2.2
                )
            )
            + (float(relevance["provider_hint"]) * 0.35)
            - ambiguity_penalty
            - canonical_mismatch_penalty
        )
        ranking_score -= track_result_penalty(
            server,
            track,
            query=query,
            normalized_anchor_artists=normalized_anchor_artists,
        )
        track["score"] = round(float(ranking_score), 3)
        track["search_source"] = source_names[0] if source_names else ""
        track["search_sources"] = source_names
        track["ranking_features"] = {
            "source_score": round(float(source_score), 4),
            "retrieval_votes": round(float(retrieval_votes), 4),
            "lexical": round(float(lexical_score), 4),
            "title_lexical": round(float(title_lexical), 4),
            "exact_title_match": round(float(exact_title_match), 4),
            "anchor_artist_match": round(float(anchor_artist_match), 4),
            "source_authority_bonus": round(float(authority_bonus), 4),
            "popularity": round(float(popularity_bonus), 4),
            "ambiguity_penalty": round(float(ambiguity_penalty), 4),
            "canonical_title_match": round(float(canonical_title_match), 4),
            "canonical_artist_match": round(float(canonical_artist_match), 4),
            "canonical_pair_match": round(float(canonical_pair_match), 4),
            "canonical_mismatch_penalty": round(
                float(canonical_mismatch_penalty),
                4,
            ),
            "query_relevance": round(float(relevance["combined"]), 4),
            "relationship_match": round(float(relevance["relationship"]), 4),
            "provider_hint": round(float(relevance["provider_hint"]), 4),
            "title_token_overlap": round(
                float(relevance["title_token_overlap"]),
                4,
            ),
            "artist_mentioned": round(
                float(relevance["artist_mentioned"]),
                4,
            ),
        }
        track["ml_similarities"] = {
            "lexical": round(float(lexical_score + title_lexical), 4),
            "anchor_artist_match": round(float(anchor_artist_match), 4),
        }
        ranked.append(track)
    return _finalize_ranked_tracks(
        server,
        ranked,
        limit=limit,
        query_intent=str(retrieval_payload.get("query_intent") or ""),
    )


def rank_artist_candidates_fast_path(
    server: Any,
    req,
    retrieval_payload: Dict[str, Any],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    server = adapt_search_server(server)
    query = server.trim_text(req.query)
    canonical_resolution = dict(
        retrieval_payload.get("canonical_resolution") or {}
    )
    resolved_artist = dict(retrieval_payload.get("resolved_artist") or {})
    canonical_artist = server.trim_text(
        resolved_artist.get("name")
        or resolved_artist.get("artist")
        or canonical_resolution.get("artist")
    )
    raw_query = " ".join(
        re.sub(
            r"\s*-\s*topic$|\s*vevo$",
            "",
            unicodedata.normalize("NFKC", query).casefold(),
        ).split()
    )
    ranked: List[Dict[str, Any]] = []
    for entry in (retrieval_payload.get("artist_candidates") or {}).values():
        payload = dict((entry or {}).get("payload") or {})
        name = server.trim_text(
            payload.get("name") or payload.get("artist") or payload.get("title")
        )
        relevance = search_text_similarity(query, name)
        raw_name = " ".join(
            re.sub(
                r"\s*-\s*topic$|\s*vevo$",
                "",
                unicodedata.normalize("NFKC", name).casefold(),
            ).split()
        )
        exact_raw_name = bool(raw_query and raw_query == raw_name)
        source_scores = dict((entry or {}).get("source_scores") or {})
        relationship_match = bool(
            set(source_scores) & {"credited_artist", "resolved_artist"}
        )
        canonical_artist_match = (
            search_text_similarity(canonical_artist, name)
            if canonical_artist
            else 0.0
        )
        if not name or (relevance < 0.58 and not relationship_match):
            continue
        source_score = max(
            (float(value or 0.0) for value in source_scores.values()),
            default=0.0,
        )
        payload["score"] = round(
            relevance * 8.0
            + (12.0 if exact_raw_name else 0.0)
            + source_score * 1.2
            + (4.5 if relationship_match else 0.0)
            + canonical_artist_match * 5.5
            + normalized_popularity(payload) * 1.1
            - artist_result_penalty(server, payload, query=query),
            3,
        )
        payload["query_relevance"] = round(relevance, 4)
        payload["canonical_artist_match"] = round(canonical_artist_match, 4)
        ranked.append(payload)
    ranked.sort(
        key=lambda item: (
            float(item.get("score") or 0.0),
            item.get("name") or "",
        ),
        reverse=True,
    )
    safe_limit = max(int(limit or 0), 0)
    if safe_limit == 0:
        return []
    results: List[Dict[str, Any]] = []
    seen_names: set[str] = set()
    for artist in ranked:
        name_key = normalize_artist_name(artist.get("name") or artist.get("artist"))
        if name_key and name_key in seen_names:
            continue
        if name_key:
            seen_names.add(name_key)
        results.append(artist)
        if len(results) >= safe_limit:
            break
    return results
def rank_album_candidates_fast_path(
    server: Any,
    req,
    retrieval_payload: Dict[str, Any],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    server = adapt_search_server(server)
    query = server.trim_text(req.query)
    ranked: List[Dict[str, Any]] = []
    for entry in (retrieval_payload.get("album_candidates") or {}).values():
        payload = dict((entry or {}).get("payload") or {})
        title = server.trim_text(payload.get("title") or payload.get("name"))
        artist = server.trim_text(payload.get("artist") or payload.get("channel"))
        title_relevance = search_text_similarity(query, title)
        combined_relevance = search_text_similarity(
            query,
            f"{title} {artist}".strip(),
        )
        relevance = max(title_relevance, combined_relevance * 0.94)
        source_scores = dict((entry or {}).get("source_scores") or {})
        relationship_match = bool(
            set(source_scores)
            & {
                "track_album",
                "artist_discography",
                "credited_artist_discography",
                "resolved_album",
            }
        )
        if not title or (relevance < 0.58 and not relationship_match):
            continue
        source_score = max(
            (float(value or 0.0) for value in source_scores.values()),
            default=0.0,
        )
        payload["score"] = round(
            relevance * 8.0
            + source_score * 1.2
            + (3.2 if relationship_match else 0.0)
            + normalized_popularity(payload)
            - album_result_penalty(server, payload, query=query),
            3,
        )
        payload["query_relevance"] = round(relevance, 4)
        payload["search_sources"] = sorted(source_scores)
        ranked.append(payload)
    ranked.sort(
        key=lambda item: (
            float(item.get("score") or 0.0),
            item.get("title") or "",
        ),
        reverse=True,
    )
    return ranked[: max(int(limit or 0), 0)]
