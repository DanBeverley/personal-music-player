from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from .feature_layer import candidate_catalog_alignment
from .home_config import _HOME_MIX_MAX_COUNT, _HOME_MIX_MIN_COUNT
from .pool_runtime import (
    _candidate_copy,
    _candidate_signature,
    _combine_pools,
    _post_filter_row_candidates,
    _row_affinity_profile,
    _row_candidate_evidence,
)
from .row_item_finalizer import finalize_row_items


def display_token(value: str) -> str:
    normalized = re.sub(r"[_\-]+", " ", str(value or "").strip())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return ""
    return normalized.title()


def genre_tab_identifier(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(label or "").strip().lower()).strip("_")


def sorted_display_tokens(values: Iterable[str], *, limit: int) -> List[str]:
    output: List[str] = []
    seen = set()
    for raw_value in sorted(
        {
            display_token(str(value or ""))
            for value in values or []
            if str(value or "").strip()
        }
    ):
        normalized = raw_value.lower()
        if not raw_value or normalized in seen:
            continue
        seen.add(normalized)
        output.append(raw_value)
        if len(output) >= limit:
            break
    return output


def trending_taste_facets(server: Any, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    affinity = _row_affinity_profile(server, profile)
    facets: List[Dict[str, Any]] = []
    seen_ids = set()

    def add_facet(kind: str, label: str, score: float) -> None:
        display = display_token(label)
        if not display:
            return
        facet_id = f"{kind}:{genre_tab_identifier(display)}"
        if not facet_id or facet_id in seen_ids:
            return
        seen_ids.add(facet_id)
        facets.append(
            {
                "id": facet_id,
                "kind": kind,
                "label": display,
                "score": float(score),
            }
        )

    def add_ranked_facets(
        kind: str,
        scores: Dict[str, Any] | None,
        *,
        limit: int,
        threshold: float,
        base_score: float,
    ) -> None:
        ordered = sorted(
            [
                (str(key or "").strip(), float(value or 0.0))
                for key, value in dict(scores or {}).items()
                if str(key or "").strip()
            ],
            key=lambda item: (-item[1], item[0]),
        )
        for label, score in ordered[: max(limit * 2, limit)]:
            if score < threshold:
                continue
            add_facet(kind, label, base_score + score)
            if len([facet for facet in facets if str(facet.get("kind") or "") == kind]) >= limit:
                break

    add_ranked_facets(
        "genre",
        affinity.get("genre_scores") or {},
        limit=5,
        threshold=0.06,
        base_score=3.0,
    )
    add_ranked_facets(
        "subgenre",
        affinity.get("subgenre_scores") or {},
        limit=4,
        threshold=0.05,
        base_score=2.6,
    )
    add_ranked_facets(
        "era",
        affinity.get("era_scores") or {},
        limit=4,
        threshold=0.06,
        base_score=2.1,
    )
    for genre_name in sorted_display_tokens(
        affinity.get("preferred_genres") or set(),
        limit=5,
    ):
        add_facet("genre", genre_name, 2.9)
    for subgenre_name in sorted_display_tokens(
        affinity.get("preferred_subgenres") or set(),
        limit=4,
    ):
        add_facet("subgenre", subgenre_name, 2.55)
    dominant_era = display_token(str(affinity.get("dominant_era") or ""))
    if dominant_era:
        add_facet("era", dominant_era, 2.15)
    for era_name in sorted_display_tokens(
        affinity.get("supported_eras") or set(),
        limit=4,
    ):
        add_facet("era", era_name, 1.95)
    return sorted(
        facets,
        key=lambda facet: (-float(facet.get("score") or 0.0), str(facet.get("label") or "")),
    )


def trending_facet_candidates(
    *,
    server: Any,
    profile: Dict[str, Any],
    facet: Dict[str, Any],
    candidates: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    facet_kind = str(facet.get("kind") or "").strip()
    facet_label = str(facet.get("label") or "").strip()
    facet_key = server._normalize_text(facet_label)
    if not facet_kind or not facet_key:
        return []

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        copied = _candidate_copy(candidate)
        track = copied.get("track") if isinstance(copied.get("track"), dict) else {}
        if not track:
            continue
        alignment = candidate_catalog_alignment(server, track, profile)
        evidence = _row_candidate_evidence(server, profile, copied)
        match_score = 0.0
        if facet_kind == "genre":
            primary = server._normalize_text(alignment.get("primary_genre") or "")
            secondary = {
                server._normalize_text(value)
                for value in list(alignment.get("secondary_genres") or [])[:4]
            }
            if primary == facet_key:
                match_score = 2.4
            elif facet_key in secondary:
                match_score = 1.7
            else:
                continue
            match_score += float(alignment.get("genre_affinity") or 0.0) * 1.0
        elif facet_kind == "subgenre":
            subgenre = server._normalize_text(alignment.get("subgenre") or "")
            if subgenre != facet_key:
                continue
            match_score = 2.2 + float(alignment.get("subgenre_affinity") or 0.0) * 1.1
        elif facet_kind == "era":
            era_bucket = server._normalize_text(alignment.get("era_bucket") or "")
            if era_bucket != facet_key:
                continue
            match_score = (
                2.0
                + float(alignment.get("era_affinity") or 0.0) * 1.0
                + float(alignment.get("adjacent_era_affinity") or 0.0) * 0.5
            )
        else:
            continue
        match_score += float(evidence.get("scene_affinity") or 0.0) * 0.55
        match_score += float(evidence.get("peer_scene_bonus") or 0.0) * 0.45
        match_score += float(copied.get("source_score") or copied.get("generator_score") or 0.0) * 0.06
        scored.append((match_score, copied))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _score, candidate in scored]


def track_artist_label(track: Dict[str, Any]) -> str:
    return str(
        track.get("channel") or track.get("artist") or track.get("author") or ""
    ).strip()


def mix_artist_line(tracks: Sequence[Dict[str, Any]], *, limit: int = 3) -> str:
    artist_names: List[str] = []
    seen = set()
    for track in tracks or []:
        artist_name = track_artist_label(track)
        normalized = artist_name.lower()
        if not artist_name or normalized in seen:
            continue
        seen.add(normalized)
        artist_names.append(artist_name)
        if len(artist_names) >= limit:
            break
    if not artist_names:
        return "Picked from the lane your listening is leaning toward."
    return ", ".join(artist_names)


def theme_accent(seed: str, palette: Sequence[str]) -> str:
    if not palette:
        return "#5B6770"
    return list(palette)[sum(ord(char) for char in str(seed or "")) % len(palette)]


def mix_rotation_seed(profile: Dict[str, Any]) -> int:
    payload = (
        f"{profile.get('profile_key') or profile.get('user_scope_id') or 'guest'}|"
        f"{time.strftime('%Y-%m-%d', time.gmtime())}"
    )
    return int(hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8], 16)


def mix_anchor_artists(server: Any, profile: Dict[str, Any], *, limit: int = 5) -> List[str]:
    artist_names: List[str] = []
    seen = set()
    sources: List[str] = []
    for track in [
        *(profile.get("last_played_tracks") or []),
        *(profile.get("recent_track_snapshots") or []),
        *(profile.get("top_track_snapshots") or []),
    ]:
        if not isinstance(track, dict):
            continue
        artist_name = track_artist_label(track)
        if artist_name:
            sources.append(artist_name)
    sources.extend(list(profile.get("top_artists") or []))
    sources.extend(list(profile.get("artist_hints") or []))
    sources.extend(list(profile.get("listened_artists") or []))
    for raw_name in sources:
        artist_name = str(raw_name or "").strip()
        normalized = server._normalize_text(artist_name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        artist_names.append(artist_name)
        if len(artist_names) >= limit:
            break
    return artist_names


def mix_anchor_genres(server: Any, profile: Dict[str, Any], *, limit: int = 4) -> List[str]:
    preferred = [
        display_token(value)
        for value in list((_row_affinity_profile(server, profile).get("preferred_genres") or []))
    ]
    genres: List[str] = []
    seen = set()
    for genre_name in preferred:
        normalized = server._normalize_text(genre_name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        genres.append(genre_name)
        if len(genres) >= limit:
            break
    return genres


def mix_blueprints(
    server: Any,
    profile: Dict[str, Any],
) -> List[Dict[str, Any]]:
    blueprints: List[Dict[str, Any]] = []
    for artist_name in mix_anchor_artists(server, profile):
        normalized = server._normalize_text(artist_name)
        blueprints.append(
            {
                "id": f"artist:{normalized}",
                "anchor_type": "artist",
                "label": artist_name,
                "ranking_row_kind": "because_you_played",
                "pool_names": (
                    "history_recent",
                    "history_top",
                    "artist_neighbors",
                    "primary_anchor_neighbors",
                    "anchor_neighbors",
                    "peer_scene",
                    "genre_subgenre",
                    "collaborative",
                ),
                "accent_seed": artist_name,
            }
        )
    for genre_name in mix_anchor_genres(server, profile):
        normalized = server._normalize_text(genre_name)
        blueprints.append(
            {
                "id": f"genre:{normalized}",
                "anchor_type": "genre",
                "label": genre_name,
                "ranking_row_kind": "trending_for_you",
                "pool_names": (
                    "peer_scene",
                    "genre_subgenre",
                    "popularity_taste",
                    "collaborative",
                    "artist_neighbors",
                    "exploration",
                ),
                "accent_seed": genre_name,
            }
        )
    return blueprints


def select_mix_blueprints(
    server: Any,
    profile: Dict[str, Any],
    blueprints: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    available = [dict(blueprint) for blueprint in list(blueprints or []) if isinstance(blueprint, dict)]
    if not available:
        return []
    desired_count = min(len(available), _HOME_MIX_MAX_COUNT)
    if len(available) > _HOME_MIX_MIN_COUNT:
        rotation_seed = mix_rotation_seed(profile)
        desired_count = min(
            len(available),
            _HOME_MIX_MIN_COUNT + (rotation_seed % max(1, (_HOME_MIX_MAX_COUNT - _HOME_MIX_MIN_COUNT + 1))),
        )
    desired_count = max(min(desired_count, len(available)), min(_HOME_MIX_MIN_COUNT, len(available)))
    rotation_offset = mix_rotation_seed(profile) % len(available)
    rotated = available[rotation_offset:] + available[:rotation_offset]
    selected: List[Dict[str, Any]] = []
    used_labels = set()
    for blueprint in rotated:
        label_key = server._normalize_text(blueprint.get("label") or "")
        if label_key and label_key in used_labels:
            continue
        if label_key:
            used_labels.add(label_key)
        selected.append(blueprint)
        if len(selected) >= desired_count:
            break
    return selected


def mix_blueprint_candidates(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    blueprint: Dict[str, Any],
    custom_row_candidates_fn=None,
) -> List[Dict[str, Any]]:
    custom_row_candidates_fn = custom_row_candidates_fn or custom_row_candidates
    ranking_row_kind = str(blueprint.get("ranking_row_kind") or "trending_for_you")
    anchor_type = str(blueprint.get("anchor_type") or "").strip()
    anchor_label = str(blueprint.get("label") or "").strip()
    anchor_key = server._normalize_text(anchor_label)
    base_candidates = custom_row_candidates_fn(
        server=server,
        profile=profile,
        snapshot=snapshot,
        row_kind=ranking_row_kind,
        pool_names=tuple(blueprint.get("pool_names") or ()),
        limit=96,
        relaxed=False,
    )
    if not base_candidates:
        return []
    strict: List[Dict[str, Any]] = []
    supportive: List[Dict[str, Any]] = []
    seen = set()
    for candidate in base_candidates:
        if not isinstance(candidate, dict):
            continue
        copied = _candidate_copy(candidate)
        signature = _candidate_signature(server, copied)
        if not signature or signature in seen:
            continue
        seen.add(signature)
        track = copied.get("track") if isinstance(copied.get("track"), dict) else {}
        evidence = _row_candidate_evidence(server, profile, copied)
        if anchor_type == "artist":
            track_artist = server._normalize_text(track_artist_label(track))
            source_names = set(evidence.get("source_names") or set())
            artist_neighbor_like = any(
                token in source_name
                for source_name in source_names
                for token in ("same_artist", "artist_neighbors", "primary_anchor_neighbors", "anchor_neighbors")
            )
            if track_artist and track_artist == anchor_key:
                strict.append(copied)
            elif artist_neighbor_like and (
                evidence["scene_affinity"] >= 0.35
                or evidence["peer_scene_bonus"] > 0.0
                or evidence["genre_affinity"] >= 0.42
            ):
                strict.append(copied)
            elif (
                evidence["scene_affinity"] >= 0.48
                or evidence["peer_scene_bonus"] > 0.0
                or evidence["genre_affinity"] >= 0.58
            ):
                supportive.append(copied)
        elif anchor_type == "genre":
            alignment = candidate_catalog_alignment(server, track, profile)
            genre_tokens = {
                server._normalize_text(alignment.get("primary_genre") or ""),
                *[
                    server._normalize_text(value)
                    for value in list(alignment.get("secondary_genres") or [])[:3]
                ],
            }
            genre_tokens.discard("")
            if anchor_key and anchor_key in genre_tokens:
                strict.append(copied)
            elif (
                evidence["scene_affinity"] >= 0.55
                or evidence["peer_scene_bonus"] > 0.0
                or evidence["genre_affinity"] >= 0.62
                or evidence["popularity_taste_fit"] >= 0.65
            ):
                supportive.append(copied)
        else:
            strict.append(copied)
    combined = strict + supportive
    return combined[:96]


def custom_row_candidates(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    row_kind: str,
    pool_names: Sequence[str],
    limit: int,
    relaxed: bool = False,
    combine_pools_fn=None,
    post_filter_row_candidates_fn=None,
) -> List[Dict[str, Any]]:
    combine_pools_fn = combine_pools_fn or _combine_pools
    post_filter_row_candidates_fn = (
        post_filter_row_candidates_fn or _post_filter_row_candidates
    )
    candidates, _ = combine_pools_fn(
        server,
        snapshot,
        tuple(pool_names),
        limit=max(int(limit or 0), 1),
    )
    return post_filter_row_candidates_fn(
        server,
        row_kind,
        profile,
        candidates,
        relaxed=relaxed,
    )


def finalize_custom_track_items(
    *,
    server: Any,
    profile: Dict[str, Any],
    ranking_row_kind: str,
    title: str,
    candidates: Sequence[Dict[str, Any]],
    limit: int,
    used_track_ids: set[str] | None = None,
    finalize_row_items_fn=None,
) -> List[Dict[str, Any]]:
    finalize_row_items_fn = finalize_row_items_fn or finalize_row_items
    finalized = finalize_row_items_fn(
        server=server,
        row_kind=ranking_row_kind,
        title=title,
        candidates=list(candidates or []),
        profile=profile,
        used_track_ids=set(used_track_ids or set()),
        used_artist_counts={},
        enforce_feed_artist_cap=False,
        max_items=max(int(limit or 0), 1),
    )
    return list((finalized or {}).get("items") or [])
