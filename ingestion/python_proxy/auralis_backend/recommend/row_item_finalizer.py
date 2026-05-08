from __future__ import annotations

from collections import defaultdict
import re
from typing import Any, Dict, Iterable, List, Tuple

from ..domain.catalog import normalize_artist_name, normalize_track_title
from .row_registry import max_feed_same_artist
from .row_ranking import (
    is_query_derived_source,
    max_same_artist,
    min_items as row_min_items,
    quality_floor,
    track_score,
)


_UNOFFICIAL_ARTIST_TOKEN_RE = re.compile(
    r"\b(tribute|karaoke|cover|covers|revival|experience|orchestra|ensemble|project)\b",
    re.IGNORECASE,
)
_UNOFFICIAL_TRACK_TOKEN_RE = re.compile(
    r"\b(tribute|karaoke|cover|instrumental|soundalike)\b",
    re.IGNORECASE,
)


def _track_artist_label(track: Dict[str, Any]) -> str:
    return str(
        track.get("channel") or track.get("artist") or track.get("author") or ""
    ).strip()


def _artist_family_identity(value: Any) -> str:
    normalized = normalize_artist_name(value)
    if not normalized:
        return ""
    family = _UNOFFICIAL_ARTIST_TOKEN_RE.sub(" ", normalized)
    family = re.sub(r"\bband\b$", " ", family).strip()
    family = re.sub(r"\s+", " ", family).strip()
    return family or normalized


def _track_authenticity_penalty(track: Dict[str, Any]) -> float:
    artist_name = _track_artist_label(track)
    title = str(track.get("title") or track.get("name") or "").strip()
    artist_normalized = normalize_artist_name(artist_name)
    title_normalized = normalize_track_title(title)
    penalty = 0.0
    if _UNOFFICIAL_ARTIST_TOKEN_RE.search(artist_normalized):
        penalty += 1.3
    if _UNOFFICIAL_TRACK_TOKEN_RE.search(title_normalized):
        penalty += 0.25
    return penalty


def _artist_family_caps(
    row_kind: str,
    *,
    max_same_artist_value: int,
    max_feed_same_artist_value: int,
) -> Tuple[int, int]:
    if row_kind in {
        "because_you_played",
        "rediscover",
        "trending_by_genre",
        "mixed_for_you",
    }:
        return (1, 1)
    return (
        max(1, int(max_same_artist_value or 1)),
        max(1, int(max_feed_same_artist_value or 1)),
    )


def finalize_row_items(
    *,
    server: Any,
    row_kind: str,
    title: str,
    candidates: Iterable[Dict[str, Any]],
    profile: Dict[str, Any],
    used_track_ids: set[str],
    used_artist_counts: Dict[str, int] | None = None,
    enforce_feed_artist_cap: bool = True,
    max_items: int = 18,
    embedding_lookup: Dict[str, List[float]] | None = None,
    metadata_enrich_limit: int | None = None,
    track_score_fn=track_score,
) -> Dict[str, Any] | None:
    if not candidates:
        return None

    input_count = len([candidate for candidate in candidates if isinstance(candidate, dict)])
    source_counts = defaultdict(int)
    merged_candidates = {}
    for raw_candidate in candidates:
        if not isinstance(raw_candidate, dict):
            continue
        raw_track = (
            raw_candidate.get("track")
            if isinstance(raw_candidate.get("track"), dict)
            else raw_candidate
        )
        normalized_track = server.normalize_recommendation_track(raw_track)
        if normalized_track is None:
            continue
        if isinstance(raw_track, dict):
            normalized_track = server._merge_track_metadata(raw_track, normalized_track)
        track_signature = server._recommendation_track_signature(normalized_track)
        if not track_signature:
            continue
        source_name = (
            server._recommendation_trim_text(
                raw_candidate.get("generator_name")
                or (raw_track.get("generator_name") if isinstance(raw_track, dict) else "")
            )
            or "candidate_pool"
        )
        source_score = float(
            raw_candidate.get("generator_score")
            or (raw_track.get("generator_score") if isinstance(raw_track, dict) else 0.0)
            or 0.0
        )
        reason = server._recommendation_trim_text(
            raw_candidate.get("reason")
            or (raw_track.get("recommendation_reason") if isinstance(raw_track, dict) else "")
        )
        source_counts[source_name] += 1
        current = merged_candidates.get(track_signature)
        if current is None:
            merged_candidates[track_signature] = {
                "track": normalized_track,
                "source_score": source_score,
                "source_votes": 1,
                "primary_source": source_name,
                "source_names": {source_name},
                "reasons": [reason] if reason else [],
            }
            continue
        current["source_votes"] = int(current.get("source_votes") or 0) + 1
        current["source_names"].add(source_name)
        if source_score > float(current.get("source_score") or 0.0):
            current["source_score"] = source_score
            current["primary_source"] = source_name
        if reason and reason not in current["reasons"] and len(current["reasons"]) < 4:
            current["reasons"].append(reason)
        if server._track_metadata_incomplete(current.get("track")):
            current["track"] = server._merge_track_metadata(
                current["track"],
                normalized_track,
            )

    if not merged_candidates:
        return None

    candidate_embeddings = dict(embedding_lookup or {})
    missing_embedding_tracks = []
    for candidate in merged_candidates.values():
        track = candidate.get("track")
        candidate_key = server._recommendation_track_embedding_key(track)
        if not candidate_key:
            continue
        if candidate_key not in candidate_embeddings:
            missing_embedding_tracks.append(track)
    if missing_embedding_tracks:
        candidate_embeddings.update(
            server._recommendation_track_embeddings(missing_embedding_tracks)
        )

    ranked = []
    model_key = "home_global_ranker_v4"
    model_version = server._ranking_model_version(model_key)
    for candidate in merged_candidates.values():
        candidate_key = server._recommendation_track_embedding_key(candidate.get("track"))
        candidate_vector = candidate_embeddings.get(candidate_key) or []
        score_payload = track_score_fn(
            server,
            candidate,
            profile,
            row_kind,
            candidate_vector,
        )
        candidate_score = float(score_payload.get("score") or 0.0)
        source_names = candidate.get("source_names") or set()
        query_derived_votes = sum(
            1 for source_name in source_names if is_query_derived_source(server, source_name)
        )
        same_artist_votes = sum(
            1
            for source_name in source_names
            if str(source_name or "").strip() == "same_artist"
        )
        if query_derived_votes > 0:
            candidate_score -= min(0.35 * query_derived_votes, 0.9)
        if row_kind in {
            "because_you_played",
            "rediscover",
            "mixed_for_you",
            "trending_by_genre",
        }:
            candidate_score -= min(0.55 * same_artist_votes, 0.85)
        authenticity_penalty = _track_authenticity_penalty(
            candidate.get("track") if isinstance(candidate.get("track"), dict) else {}
        )
        if authenticity_penalty > 0.0:
            candidate_score -= authenticity_penalty
            ranking_features = dict(score_payload.get("ranking_features") or {})
            ranking_features["authenticity_penalty"] = -authenticity_penalty
            score_payload["ranking_features"] = ranking_features
        if same_artist_votes > 0 and row_kind in {
            "because_you_played",
            "rediscover",
            "mixed_for_you",
            "trending_by_genre",
        }:
            ranking_features = dict(score_payload.get("ranking_features") or {})
            ranking_features["same_artist_source_penalty"] = -min(
                0.55 * same_artist_votes,
                0.85,
            )
            score_payload["ranking_features"] = ranking_features
        ranked.append((candidate_score, candidate, score_payload))
    ranked.sort(key=lambda item: item[0], reverse=True)

    selected = []
    artist_counts = {}
    global_artist_counts = used_artist_counts if isinstance(used_artist_counts, dict) else {}
    artist_family_counts = {}
    global_artist_family_counts = global_artist_counts
    query_derived_selected = 0
    quality_floor_value = quality_floor(row_kind)
    max_same_artist_value = max_same_artist(row_kind)
    max_feed_same_artist_value = max_feed_same_artist(row_kind)
    max_same_family_value, max_feed_same_family_value = _artist_family_caps(
        row_kind,
        max_same_artist_value=max_same_artist_value,
        max_feed_same_artist_value=max_feed_same_artist_value,
    )
    minimum_items = row_min_items(row_kind)
    query_derived_limit = min(
        server.RECOMMENDATION_QUERY_DERIVED_SOURCE_ITEM_CAP,
        max(2, int(max_items * server.RECOMMENDATION_QUERY_DERIVED_SOURCE_SHARE_CAP)),
    )

    for candidate_score, candidate, score_payload in ranked:
        if candidate_score < quality_floor_value:
            continue
        track = dict(candidate["track"])
        track_key = server._recommendation_track_signature(track)
        if not track_key or track_key in used_track_ids:
            continue
        artist_key = server._normalize_text(
            track.get("channel") or track.get("artist") or track.get("author") or ""
        )
        artist_family_key = _artist_family_identity(
            track.get("channel") or track.get("artist") or track.get("author") or ""
        )
        feed_count = 0
        strict_row_family_caps = row_kind in {
            "because_you_played",
            "rediscover",
            "mixed_for_you",
            "trending_by_genre",
        }
        if artist_key:
            current_count = artist_counts.get(artist_key, 0)
            if current_count >= max_same_artist_value and (
                strict_row_family_caps or len(selected) + 1 < max_items
            ):
                continue
            feed_count = int(global_artist_counts.get(artist_key) or 0)
            if (
                enforce_feed_artist_cap
                and feed_count >= max_feed_same_artist_value
                and (strict_row_family_caps or len(selected) + 1 < max_items)
            ):
                continue
        if artist_family_key:
            current_family_count = int(artist_family_counts.get(artist_family_key) or 0)
            if current_family_count >= max_same_family_value and (
                strict_row_family_caps or len(selected) + 1 < max_items
            ):
                continue
            global_family_count = int(
                global_artist_family_counts.get(f"family:{artist_family_key}") or 0
            )
            if (
                enforce_feed_artist_cap
                and global_family_count >= max_feed_same_family_value
                and (strict_row_family_caps or len(selected) + 1 < max_items)
            ):
                continue
        source_names = sorted(candidate.get("source_names") or [])
        is_query_derived = any(
            is_query_derived_source(server, source_name)
            for source_name in source_names
        )
        if (
            is_query_derived
            and query_derived_selected >= query_derived_limit
            and len(selected) + 1 < max_items
        ):
            continue
        if is_query_derived:
            query_derived_selected += 1
        track["generator_score"] = round(candidate_score, 3)
        track["ml_similarities"] = dict(score_payload.get("ml_similarities") or {})
        track["ranking_features"] = {
            key: round(float(value), 4)
            for key, value in (score_payload.get("ranking_features") or {}).items()
        }
        track["recommendation_source"] = candidate.get("primary_source") or ""
        track["recommendation_sources"] = source_names
        if candidate.get("reasons"):
            track["recommendation_reason"] = candidate["reasons"][0]
        track["ranking_model"] = {
            "key": score_payload.get("model_key") or model_key,
            "version": score_payload.get("model_version") or model_version,
        }
        selected.append(track)
        if artist_key:
            artist_counts[artist_key] = int(artist_counts.get(artist_key) or 0) + 1
            global_artist_counts[artist_key] = feed_count + 1
        if artist_family_key:
            artist_family_counts[artist_family_key] = (
                int(artist_family_counts.get(artist_family_key) or 0) + 1
            )
            global_artist_family_counts[f"family:{artist_family_key}"] = (
                int(
                    global_artist_family_counts.get(f"family:{artist_family_key}") or 0
                )
                + 1
            )
        used_track_ids.add(track_key)
        if len(selected) >= max_items:
            break

    if len(selected) < minimum_items:
        return None

    incomplete_indexes = [
        index
        for index, track in enumerate(selected)
        if server._track_metadata_incomplete(track)
    ]
    if incomplete_indexes and metadata_enrich_limit != 0:
        if metadata_enrich_limit is not None and metadata_enrich_limit > 0:
            incomplete_indexes = incomplete_indexes[:metadata_enrich_limit]
        futures = {
            index: server.recommendation_executor.submit(
                server._recommendation_enrich_track_metadata,
                selected[index],
            )
            for index in incomplete_indexes
        }
        for index, future in futures.items():
            try:
                enriched = future.result(
                    timeout=server.RECOMMENDATION_METADATA_ENRICH_PER_TRACK_TIMEOUT_SECONDS
                )
            except Exception:
                enriched = None
            if enriched is not None:
                preserved = dict(selected[index])
                merged = server._merge_track_metadata(preserved, enriched)
                for key in (
                    "generator_score",
                    "ml_similarities",
                    "ranking_features",
                    "recommendation_source",
                    "recommendation_sources",
                    "recommendation_reason",
                    "ranking_model",
                ):
                    if key in preserved:
                        merged[key] = preserved[key]
                selected[index] = merged

    selected_source_counts = defaultdict(int)
    for track in selected:
        for source_name in track.get("recommendation_sources") or []:
            selected_source_counts[source_name] += 1

    row_feature_mix_totals: Dict[str, float] = defaultdict(float)
    row_feature_mix_counts: Dict[str, int] = defaultdict(int)
    for track in selected:
        for feature_name, feature_value in dict(track.get("ranking_features") or {}).items():
            row_feature_mix_totals[feature_name] += float(feature_value or 0.0)
            row_feature_mix_counts[feature_name] += 1
    row_feature_mix = {
        feature_name: round(
            row_feature_mix_totals[feature_name]
            / max(row_feature_mix_counts[feature_name], 1),
            4,
        )
        for feature_name in row_feature_mix_totals.keys()
        if row_feature_mix_counts.get(feature_name)
    }

    return {
        "id": row_kind,
        "kind": row_kind,
        "title": title,
        "items": selected,
        "_diagnostics": {
            "model_key": model_key,
            "model_version": model_version,
            "quality_floor": round(float(quality_floor_value), 4),
            "candidate_count_input": input_count,
            "candidate_count_merged": len(merged_candidates),
            "selected_count": len(selected),
            "source_counts": dict(source_counts),
            "selected_source_counts": dict(selected_source_counts),
            "row_feature_mix": row_feature_mix,
        },
    }


def apply_track_row_runtime_fields(
    *,
    server: Any,
    finalized: Dict[str, Any],
    row_seed: Dict[str, Any],
) -> Dict[str, Any]:
    updated = dict(finalized or {})
    row_kind = server._recommendation_trim_text(
        row_seed.get("kind") or updated.get("kind")
    )
    candidate_count = len(row_seed.get("candidates") or [])
    item_count = len(updated.get("items") or [])
    updated["extension_cycle"] = 0
    updated["can_extend"] = candidate_count > item_count or row_kind in {
        "continue_listening",
        "because_you_played",
        "trending_for_you",
        "quiet_picks",
        "deep_cuts",
        "rediscover",
    }
    updated["used_signatures"] = [
        signature
        for signature in (
            server._recommendation_track_signature(track)
            for track in (updated.get("items") or [])
        )
        if signature
    ]
    return updated


def apply_quiet_row_runtime_fields(
    *,
    server: Any,
    finalized: Dict[str, Any],
    row_seed: Dict[str, Any],
) -> Dict[str, Any]:
    updated = apply_track_row_runtime_fields(
        server=server,
        finalized=finalized,
        row_seed=row_seed,
    )
    updated["base_query"] = server._recommendation_trim_text(
        row_seed.get("quiet_query")
    )
    initial_used_queries = [
        query
        for query in (row_seed.get("used_queries") or [])
        if server._recommendation_trim_text(query)
    ]
    if not initial_used_queries:
        quiet_query = server._recommendation_trim_text(row_seed.get("quiet_query"))
        initial_used_queries = [quiet_query] if quiet_query else []
    updated["used_queries"] = initial_used_queries
    return updated
