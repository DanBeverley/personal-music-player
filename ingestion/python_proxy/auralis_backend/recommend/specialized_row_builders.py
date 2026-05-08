from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Sequence, Tuple

from .feature_layer import candidate_catalog_alignment


def _normalize_mix_refinement_cache_entries(
    raw_entries: Sequence[Dict[str, Any]] | None,
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entry in list(raw_entries or []):
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "").strip()
        if not entry_id or entry_id in seen_ids:
            continue
        candidate_tracks = [
            dict(track)
            for track in list(entry.get("candidates") or [])
            if isinstance(track, dict)
        ]
        normalized.append(
            {
                "id": entry_id,
                "label": str(entry.get("label") or "").strip(),
                "anchor_type": str(entry.get("anchor_type") or "").strip(),
                "accent_seed": str(entry.get("accent_seed") or "").strip(),
                "ranking_row_kind": str(entry.get("ranking_row_kind") or "").strip(),
                "candidates": candidate_tracks,
            }
        )
        seen_ids.add(entry_id)
    return normalized


def _merge_trending_tabs(
    previous_tabs: Sequence[Dict[str, Any]] | None,
    next_tabs: Sequence[Dict[str, Any]] | None,
) -> List[Dict[str, Any]]:
    merged_by_id: Dict[str, Dict[str, Any]] = {}
    ordered_tab_ids: List[str] = []

    def merge_one(raw_tab: Dict[str, Any]) -> None:
        if not isinstance(raw_tab, dict):
            return
        tab_id = str(raw_tab.get("id") or "").strip()
        if not tab_id:
            return
        normalized_tab = dict(raw_tab)
        normalized_pages = [
            [
                dict(track)
                for track in list(page or [])
                if isinstance(track, dict)
            ]
            for page in list(normalized_tab.get("pages") or [])
            if isinstance(page, list)
        ]
        normalized_tab["pages"] = normalized_pages
        normalized_tab["page_count"] = len(normalized_pages)
        normalized_tab["tracks"] = [
            dict(track)
            for track in list(normalized_tab.get("tracks") or [])
            if isinstance(track, dict)
        ]
        existing_tab = merged_by_id.get(tab_id)
        if existing_tab is None:
            merged_by_id[tab_id] = normalized_tab
            ordered_tab_ids.append(tab_id)
            return
        existing_page_count = max(int(existing_tab.get("page_count") or 0), 1)
        incoming_page_count = max(int(normalized_tab.get("page_count") or 0), 1)
        existing_track_count = len(list(existing_tab.get("tracks") or []))
        incoming_track_count = len(list(normalized_tab.get("tracks") or []))
        if (
            incoming_page_count > existing_page_count
            or incoming_track_count > existing_track_count
        ):
            merged_by_id[tab_id] = normalized_tab

    for tab in list(previous_tabs or []):
        merge_one(dict(tab) if isinstance(tab, dict) else {})
    for tab in list(next_tabs or []):
        merge_one(dict(tab) if isinstance(tab, dict) else {})
    return [merged_by_id[tab_id] for tab_id in ordered_tab_ids if tab_id in merged_by_id]


def _unpack_trending_tabs_result(
    result: Any,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any] | None, Dict[str, Any]]:
    if not isinstance(result, tuple) or len(result) < 2:
        return [], None, {}
    tabs = list(result[0] or []) if isinstance(result[0], list) else []
    active_tab = dict(result[1]) if isinstance(result[1], dict) else None
    progress = dict(result[2] or {}) if len(result) >= 3 and isinstance(result[2], dict) else {}
    return tabs, active_tab, progress


def build_continue_listening_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
    combine_pools_fn: Callable[..., Tuple[List[Dict[str, Any]], Dict[str, int]]],
    snapshot_substrate_mode_fn: Callable[[Dict[str, Any] | None], str],
) -> Dict[str, Any] | None:
    pool_names = (
        "history_recent",
        "same_artist",
        "primary_anchor_neighbors",
        "anchor_neighbors",
        "artist_neighbors",
        "history_top",
        "collaborative",
        "offline_library",
    )
    candidate_limit = max(
        48,
        int((snapshot.get("pool_counts") or {}).get("history_recent") or 0),
        int((snapshot.get("pool_counts") or {}).get("history_top") or 0),
        24,
    )
    candidates, source_pool_counts = combine_pools_fn(
        server,
        snapshot,
        pool_names,
        limit=candidate_limit,
    )
    if not candidates:
        return None
    tracks: List[Dict[str, Any]] = []
    seen_signatures = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        track = candidate.get("track") if isinstance(candidate.get("track"), dict) else {}
        normalized_track = server.normalize_recommendation_track(track)
        if normalized_track is None:
            continue
        normalized_track = server._merge_track_metadata(track, normalized_track)
        signature = server._recommendation_track_signature(normalized_track)
        if not signature or signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        tracks.append(normalized_track)
        if len(tracks) >= 18:
            break
    if not tracks:
        return None
    return {
        "title": title,
        "kind": "continue_listening",
        "item_type": "track",
        "items": tracks,
        "row_strategy": (
            "core_history"
            if snapshot_substrate_mode_fn(snapshot) == "thin_core"
            else "personalized"
        ),
        "fallback_reason": "",
        "source_pool_counts": source_pool_counts,
        "allocator_model": {
            "key": "home_continue_seed_contract_v1",
            "version": "home_continue_seed_contract_v1",
        },
        "allocator_pool_order": list(pool_names),
        "allocator_pool_scores": [],
        "allocator_ms": 0,
    }


def build_todays_pick_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
    home_todays_pick_candidate_cap: int,
    custom_row_candidates_fn: Callable[..., List[Dict[str, Any]]],
    finalize_custom_track_items_fn: Callable[..., List[Dict[str, Any]]],
    track_artist_label_fn: Callable[[Dict[str, Any]], str],
    theme_accent_fn: Callable[[str, Sequence[str]], str],
) -> Dict[str, Any] | None:
    primary_candidates = custom_row_candidates_fn(
        server=server,
        profile=profile,
        snapshot=snapshot,
        row_kind="todays_pick",
        pool_names=(
            "peer_scene",
            "genre_subgenre",
            "popularity_taste",
            "collaborative",
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
        ),
        limit=home_todays_pick_candidate_cap,
        relaxed=True,
    )
    history_candidates = custom_row_candidates_fn(
        server=server,
        profile=profile,
        snapshot=snapshot,
        row_kind="todays_pick",
        pool_names=("history_top", "history_recent"),
        limit=max(8, home_todays_pick_candidate_cap // 2),
        relaxed=True,
    )
    candidates = list(primary_candidates or [])
    if not candidates:
        candidates = list(history_candidates or [])
    if not candidates:
        return None
    items = finalize_custom_track_items_fn(
        server=server,
        profile=profile,
        ranking_row_kind="todays_pick",
        title=title,
        candidates=candidates,
        limit=1,
    )
    if not items:
        return None
    featured_track = dict(items[0])
    artist_name = track_artist_label_fn(featured_track)
    reason = str(featured_track.get("recommendation_reason") or "").strip()
    if not reason:
        reason = (
            f"Strongest fit right now for {artist_name}."
            if artist_name
            else "Strongest fit right now for your current taste."
        )
    return {
        "title": title,
        "kind": "todays_pick",
        "item_type": "track",
        "row_style": "featured_track",
        "items": [featured_track],
        "meta": {
            "eyebrow": "TODAY'S PICK",
            "caption": reason,
            "accent_color": theme_accent_fn(
                featured_track.get("id") or featured_track.get("title") or title,
                (
                    "#C4511B",
                    "#B2512B",
                    "#8E3B22",
                    "#8C4A2E",
                ),
            ),
        },
        "row_strategy": "personalized",
        "fallback_reason": "",
        "source_pool_counts": {
            "candidates": len(candidates),
        },
        "allocator_ms": 0,
    }


def build_mixed_for_you_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
    home_mix_track_cap: int,
    home_mix_max_count: int,
    home_mix_accents: Sequence[str],
    select_mix_blueprints_fn: Callable[..., List[Dict[str, Any]]],
    mix_blueprints_fn: Callable[..., List[Dict[str, Any]]],
    mix_blueprint_candidates_fn: Callable[..., List[Dict[str, Any]]],
    finalize_custom_track_items_fn: Callable[..., List[Dict[str, Any]]],
    theme_accent_fn: Callable[[str, Sequence[str]], str],
    mix_artist_line_fn: Callable[[Sequence[Dict[str, Any]]], str],
    full_refinement: bool = False,
    existing_row: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    started_at = time.perf_counter()
    mix_items: List[Dict[str, Any]] = []
    used_track_ids: set[str] = set()
    minimum_track_count = max(3, min(4, home_mix_track_cap))
    fast_ready_mix_target = min(home_mix_max_count, 3)
    partial_mix_count = 0
    cache_hits = 0
    cache_misses = 0
    blueprints = select_mix_blueprints_fn(
        server,
        profile,
        mix_blueprints_fn(server, profile),
    )
    cached_entries = _normalize_mix_refinement_cache_entries(
        list(dict(snapshot or {}).get("mixed_for_you_cache", {}).get("entries") or [])
    )
    if not cached_entries and isinstance(existing_row, dict):
        cached_entries = _normalize_mix_refinement_cache_entries(
            list(
                dict((existing_row.get("meta") or {}))
                .get("mixed_for_you_cache", {})
                .get("entries")
                or []
            )
        )
    cached_entries_by_id = {
        str(entry.get("id") or "").strip(): dict(entry)
        for entry in cached_entries
        if str(entry.get("id") or "").strip()
    }
    next_cache_entries: List[Dict[str, Any]] = []
    cached_entry_ids: set[str] = set()
    mix_target = home_mix_max_count if full_refinement else fast_ready_mix_target
    for index, blueprint in enumerate(blueprints):
        blueprint_id = str(blueprint.get("id") or f"mix_{index}").strip()
        cached_entry = cached_entries_by_id.get(blueprint_id)
        if cached_entry and list(cached_entry.get("candidates") or []):
            cache_hits += 1
            candidates = [
                dict(track)
                for track in list(cached_entry.get("candidates") or [])
                if isinstance(track, dict)
            ]
        else:
            cache_misses += 1
            candidates = mix_blueprint_candidates_fn(
                server=server,
                profile=profile,
                snapshot=snapshot,
                blueprint=blueprint,
            )
        if not candidates:
            continue
        if blueprint_id not in cached_entry_ids:
            next_cache_entries.append(
                {
                    "id": blueprint_id,
                    "label": str(blueprint.get("label") or "").strip(),
                    "anchor_type": str(blueprint.get("anchor_type") or "").strip(),
                    "accent_seed": str(blueprint.get("accent_seed") or "").strip(),
                    "ranking_row_kind": str(
                        blueprint.get("ranking_row_kind") or ""
                    ).strip(),
                    "candidates": [
                        dict(track)
                        for track in list(candidates or [])[
                            : max(home_mix_track_cap * 4, 16)
                        ]
                        if isinstance(track, dict)
                    ],
                }
            )
            cached_entry_ids.add(blueprint_id)
        tracks = finalize_custom_track_items_fn(
            server=server,
            profile=profile,
            ranking_row_kind=str(blueprint.get("ranking_row_kind") or "trending_for_you"),
            title=str(blueprint.get("label") or title),
            candidates=candidates,
            limit=home_mix_track_cap,
            used_track_ids=used_track_ids,
        )
        if len(tracks) < minimum_track_count:
            continue
        if len(tracks) < min(5, home_mix_track_cap):
            partial_mix_count += 1
        for track in tracks:
            track_signature = server._recommendation_track_signature(track)
            if track_signature:
                used_track_ids.add(track_signature)
        first_track = tracks[0]
        mix_number = len(mix_items) + 1
        mix_title = f"Mix {mix_number}"
        anchor_label = str(blueprint.get("label") or "").strip()
        anchor_type = str(blueprint.get("anchor_type") or "").strip()
        if anchor_type == "artist" and anchor_label:
            description = f"Built around {anchor_label} and the artists circling the same lane."
        elif anchor_type == "genre" and anchor_label:
            description = f"Built around the {anchor_label} lane your listening keeps leaning into."
        else:
            description = "Built from the strongest lane in your current taste."
        mix_items.append(
            {
                "id": str(blueprint.get("id") or f"mix_{mix_number}"),
                "title": mix_title,
                "badge": f"MIX {mix_number}",
                "thumbnail": first_track.get("thumbnail"),
                "accent_color": theme_accent_fn(
                    str(blueprint.get("accent_seed") or first_track.get("id") or mix_title),
                    home_mix_accents,
                ),
                "subtitle": mix_artist_line_fn(tracks),
                "description": description,
                "track_count": len(tracks),
                "anchor_label": anchor_label,
                "tracks": tracks,
            }
        )
        if len(mix_items) >= mix_target:
            break
    if not mix_items:
        return None
    snapshot_cache_payload = {
        "entries": next_cache_entries[: max(home_mix_max_count * 2, 8)],
        "selected_blueprint_count": len(blueprints),
    }
    if isinstance(snapshot, dict):
        snapshot["mixed_for_you_cache"] = snapshot_cache_payload
    pending_blueprint_count = max(
        0,
        min(len(blueprints), home_mix_max_count) - len(mix_items),
    )
    refinement_exhausted = bool(full_refinement or pending_blueprint_count == 0)
    partial_ready = (
        (
            pending_blueprint_count > 0
            or partial_mix_count > 0
            or len(mix_items) < fast_ready_mix_target
        )
        if not refinement_exhausted
        else False
    )
    return {
        "title": title,
        "kind": "mixed_for_you",
        "item_type": "mix",
        "row_style": "mix_cards",
        "items": mix_items,
        "meta": {
            "eyebrow": "Made for this session",
            "build_ms": int((time.perf_counter() - started_at) * 1000),
            "mix_count": len(mix_items),
            "attempted_blueprints": len(blueprints),
            "partial_mix_count": partial_mix_count,
            "pending_blueprint_count": pending_blueprint_count,
            "refinement_exhausted": refinement_exhausted,
            "minimum_track_count": minimum_track_count,
            "fast_ready_mix_target": fast_ready_mix_target,
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "partial_ready": partial_ready,
            **(
                {
                    "loading_label": "Refining",
                    "loading_message":
                        "Blending a few more lanes into this mix set.",
                }
                if partial_ready
                else {}
            ),
        },
        "row_strategy": "personalized",
        "fallback_reason": "",
        "source_pool_counts": {
            "mixes": len(mix_items),
        },
        "allocator_ms": 0,
    }


def _chunk_track_pages(
    tracks: Sequence[Dict[str, Any]],
    *,
    page_size: int,
) -> List[List[Dict[str, Any]]]:
    bounded_page_size = max(int(page_size or 0), 1)
    return [
        list(tracks[index : index + bounded_page_size])
        for index in range(0, len(list(tracks or [])), bounded_page_size)
    ]


def _fallback_genre_facets_from_candidates(
    server: Any,
    candidates: Sequence[Dict[str, Any]],
    *,
    existing_ids: set[str],
    limit: int,
) -> List[Dict[str, Any]]:
    scored: Dict[str, Dict[str, Any]] = {}

    def add(kind: str, label: str, score: float) -> None:
        display = str(label or "").strip()
        if not display:
            return
        tab_slug = server._normalize_text(display).replace(" ", "_")
        facet_id = f"{kind}:{tab_slug}".strip(":")
        if not facet_id or facet_id in existing_ids:
            return
        current = scored.get(facet_id)
        if current is None or score > float(current.get("score") or 0.0):
            scored[facet_id] = {
                "id": facet_id,
                "kind": kind,
                "label": display.title(),
                "score": score,
                "fallback_source": "candidate_catalog",
            }

    for candidate in list(candidates or []):
        if not isinstance(candidate, dict):
            continue
        track = candidate.get("track") if isinstance(candidate.get("track"), dict) else {}
        if not track:
            continue
        alignment = candidate_catalog_alignment(server, track, {})
        primary_genre = str(alignment.get("primary_genre") or "").strip()
        subgenre = str(alignment.get("subgenre") or "").strip()
        era_bucket = str(alignment.get("era_bucket") or "").strip()
        if primary_genre:
            add("genre", primary_genre, 1.9 + float(alignment.get("genre_affinity") or 0.0))
        if subgenre:
            add("subgenre", subgenre, 1.65 + float(alignment.get("subgenre_affinity") or 0.0))
        if era_bucket:
            add("era", era_bucket, 1.4 + float(alignment.get("era_affinity") or 0.0))
    return sorted(
        scored.values(),
        key=lambda facet: (-float(facet.get("score") or 0.0), str(facet.get("label") or "")),
    )[: max(limit, 0)]


def build_trending_genre_tabs(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    preferred_tab_ids: Sequence[str] | None = None,
    selected_tab_id: str = "",
    home_genre_candidate_cap: int,
    home_genre_tab_limit: int,
    home_genre_track_cap: int,
    build_row_allocation_plan_fn: Callable[..., Dict[str, Any]],
    trending_primary_pool_order_fn: Callable[..., Sequence[str]],
    combine_pools_fn: Callable[..., Tuple[List[Dict[str, Any]], Dict[str, int]]],
    trending_taste_facets_fn: Callable[..., List[Dict[str, Any]]],
    trending_facet_candidates_fn: Callable[..., List[Dict[str, Any]]],
    finalize_custom_track_items_fn: Callable[..., List[Dict[str, Any]]],
    mix_artist_line_fn: Callable[[Sequence[Dict[str, Any]]], str],
    genre_tab_identifier_fn: Callable[[str], str],
    full_refinement: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any] | None, Dict[str, Any]]:
    minimum_tracks_per_tab = max(3, min(4, home_genre_track_cap))
    secondary_tab_minimum_tracks = (
        minimum_tracks_per_tab
        if not full_refinement
        else max(3, minimum_tracks_per_tab - 1)
    )
    fast_ready_tab_target = min(home_genre_tab_limit, 3)
    allocation_plan = build_row_allocation_plan_fn(
        server,
        row_kind="trending_for_you",
        profile=profile,
        snapshot=snapshot,
    )
    pool_names = trending_primary_pool_order_fn(snapshot, allocation_plan)
    candidates, _source_pool_counts = combine_pools_fn(
        server,
        snapshot,
        tuple(pool_names),
        limit=max(home_genre_candidate_cap * 3, 160),
    )
    if not candidates:
        return [], None

    facets = trending_taste_facets_fn(server, profile)
    facet_by_id = {
        str(facet.get("id") or "").strip(): dict(facet)
        for facet in facets
        if str(facet.get("id") or "").strip()
    }
    ordered_facets: List[Dict[str, Any]] = []
    for preferred_id in list(preferred_tab_ids or []):
        normalized_id = str(preferred_id or "").strip()
        facet = facet_by_id.get(normalized_id)
        if facet and facet not in ordered_facets:
            ordered_facets.append(facet)
    for facet in facets:
        if facet not in ordered_facets:
            ordered_facets.append(facet)
    if len(ordered_facets) < max(home_genre_tab_limit, 3):
        existing_ids = {
            str(facet.get("id") or "").strip()
            for facet in ordered_facets
            if isinstance(facet, dict)
        }
        ordered_facets.extend(
            _fallback_genre_facets_from_candidates(
                server,
                candidates,
                existing_ids=existing_ids,
                limit=max(home_genre_tab_limit * 2, 8) - len(ordered_facets),
            )
        )
    tabs: List[Dict[str, Any]] = []
    used_track_ids: set[str] = set()
    tab_target = home_genre_tab_limit if full_refinement else fast_ready_tab_target
    completion_tab_goal = max(1, min(home_genre_tab_limit, 3))
    reached_tab_target = False
    for facet in ordered_facets[: max(home_genre_tab_limit * 2, 8)]:
        facet_candidates = trending_facet_candidates_fn(
            server=server,
            profile=profile,
            facet=facet,
            candidates=candidates,
        )
        if not facet_candidates:
            continue
        tracks = finalize_custom_track_items_fn(
            server=server,
            profile=profile,
            ranking_row_kind="trending_for_you",
            title=str(facet.get("label") or "Genre"),
            candidates=facet_candidates,
            limit=max(home_genre_track_cap * 2, home_genre_track_cap + 4),
            used_track_ids=set(),
        )
        if len(tracks) < secondary_tab_minimum_tracks:
            continue
        diversified_tracks: List[Dict[str, Any]] = []
        local_artist_keys: set[str] = set()
        selected_signatures: set[str] = set()
        cross_tab_seen = used_track_ids if not full_refinement else set()
        for track in tracks:
            track_signature = server._recommendation_track_signature(track)
            artist_key = server._normalize_text(
                track.get("channel") or track.get("artist") or track.get("author") or ""
            )
            if track_signature and track_signature in cross_tab_seen:
                continue
            if (
                artist_key
                and artist_key in local_artist_keys
                and len(diversified_tracks) >= 2
            ):
                continue
            diversified_tracks.append(track)
            if track_signature:
                selected_signatures.add(track_signature)
            if artist_key:
                local_artist_keys.add(artist_key)
            if len(diversified_tracks) >= home_genre_track_cap:
                break
        if len(diversified_tracks) < minimum_tracks_per_tab:
            for track in tracks:
                track_signature = server._recommendation_track_signature(track)
                if track_signature and track_signature in selected_signatures:
                    continue
                if track_signature and track_signature in cross_tab_seen:
                    continue
                diversified_tracks.append(track)
                if track_signature:
                    selected_signatures.add(track_signature)
                if len(diversified_tracks) >= home_genre_track_cap:
                    break
        if len(diversified_tracks) < secondary_tab_minimum_tracks:
            continue
        if not full_refinement:
            for track_signature in selected_signatures:
                used_track_ids.add(track_signature)
        tab_id = str(facet.get("id") or "").strip()
        pages = _chunk_track_pages(
            diversified_tracks,
            page_size=4,
        )
        tabs.append(
            {
                "id": tab_id,
                "label": str(facet.get("label") or "Genre"),
                "facet_kind": str(facet.get("kind") or "genre"),
                "subtitle": mix_artist_line_fn(diversified_tracks),
                "tracks": diversified_tracks,
                "pages": pages,
                "page_size": 4,
                "page_count": len(pages),
            }
        )
        if len(tabs) >= tab_target:
            reached_tab_target = True
            break
    total_page_count = sum(max(int(tab.get("page_count") or 0), 1) for tab in tabs)
    progress = {
        "tab_goal": completion_tab_goal,
        "tab_target": tab_target,
        "refinement_exhausted": not reached_tab_target,
        "total_page_count": total_page_count,
        "available_facet_count": len(ordered_facets),
        "accepted_tab_count": len(tabs),
    }
    if not tabs:
        return [], None, progress
    active_tab = next(
        (
            dict(tab)
            for tab in tabs
            if str(tab.get("id") or "").strip() == genre_tab_identifier_fn(selected_tab_id)
        ),
        dict(tabs[0]),
    )
    return tabs, active_tab, progress


def build_trending_by_genre_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
    trending_genre_tabs_fn: Callable[..., Tuple[List[Dict[str, Any]], Dict[str, Any] | None]],
    full_refinement: bool = False,
    existing_row: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    started_at = time.perf_counter()
    tabs, active_tab, progress = _unpack_trending_tabs_result(
        trending_genre_tabs_fn(
        server=server,
        profile=profile,
        snapshot=snapshot,
        full_refinement=full_refinement,
        )
    )
    previous_tabs = []
    selected_tab_id = ""
    if isinstance(existing_row, dict):
        previous_meta = dict(existing_row.get("meta") or {})
        previous_tabs = [
            dict(tab)
            for tab in list(previous_meta.get("tabs") or [])
            if isinstance(tab, dict)
        ]
        selected_tab_id = str(previous_meta.get("active_tab_id") or "").strip()
    if previous_tabs:
        tabs = _merge_trending_tabs(previous_tabs, tabs)
        if selected_tab_id:
            resolved_tab_id = selected_tab_id
        else:
            resolved_tab_id = str(active_tab.get("id") or "").strip() if isinstance(active_tab, dict) else ""
        active_tab = next(
            (
                dict(tab)
                for tab in tabs
                if str(tab.get("id") or "").strip() == resolved_tab_id
            ),
            dict(tabs[0]) if tabs else None,
        )
    if not tabs or not isinstance(active_tab, dict):
        return None
    total_page_count = sum(max(int(tab.get("page_count") or 0), 1) for tab in tabs)
    tab_goal = max(1, int(progress.get("tab_goal") or 3))
    refinement_exhausted = bool(progress.get("refinement_exhausted"))
    pending_tab_count = 0 if refinement_exhausted else max(0, tab_goal - len(tabs))
    partial_ready = (
        (pending_tab_count > 0 or total_page_count < 2) and not refinement_exhausted
        if not full_refinement
        else False
    )
    return {
        "title": title,
        "kind": "trending_by_genre",
        "item_type": "track",
        "row_style": "genre_tabs",
        "items": list((active_tab.get("pages") or [active_tab.get("tracks") or []])[0] or []),
        "meta": {
            "eyebrow": "TRENDING BY GENRE",
            "active_tab_id": active_tab.get("id") or "",
            "tabs": tabs,
            "accent_color": "#245E8C",
            "page_size": 4,
            "build_ms": int((time.perf_counter() - started_at) * 1000),
            "tab_count": len(tabs),
            "tab_goal": tab_goal,
            "tab_target": int(progress.get("tab_target") or tab_goal),
            "pending_tab_count": pending_tab_count,
            "total_page_count": total_page_count,
            "refinement_exhausted": refinement_exhausted,
            "available_facet_count": int(progress.get("available_facet_count") or len(tabs)),
            "accepted_tab_count": int(progress.get("accepted_tab_count") or len(tabs)),
            "fast_ready_tab_target": min(len(tabs), 3),
            "minimum_tracks_per_tab": 3,
            "partial_ready": partial_ready,
            **(
                {
                    "loading_label": "Refining",
                    "loading_message":
                        "Adding more genre pockets around the strongest tab.",
                }
                if partial_ready
                else {}
            ),
        },
        "row_strategy": "personalized",
        "fallback_reason": "",
        "source_pool_counts": {
            "genres": len(tabs),
        },
        "allocator_ms": 0,
    }


def refresh_trending_by_genre_row_builder(
    *,
    row: Dict[str, Any],
    tab_id: str,
    trending_genre_tabs_fn: Callable[..., Tuple[List[Dict[str, Any]], Dict[str, Any] | None]],
) -> Dict[str, Any]:
    updated = dict(row or {})
    meta = dict(updated.get("meta") or {})
    previous_tabs = [
        str(tab.get("id") or "").strip()
        for tab in list(meta.get("tabs") or [])
        if isinstance(tab, dict) and str(tab.get("id") or "").strip()
    ]
    tabs, active_tab, progress = _unpack_trending_tabs_result(
        trending_genre_tabs_fn(
            preferred_tab_ids=previous_tabs,
            selected_tab_id=tab_id,
        )
    )
    if not tabs or not isinstance(active_tab, dict):
        meta["pending_tab_count"] = 0
        meta["refinement_exhausted"] = True
        meta["partial_ready"] = False
        meta.pop("loading_label", None)
        meta.pop("loading_message", None)
        updated["meta"] = meta
        updated["can_extend"] = False
        return updated
    merged_tabs = _merge_trending_tabs(
        [
            dict(tab)
            for tab in list(meta.get("tabs") or [])
            if isinstance(tab, dict)
        ],
        tabs,
    )
    if merged_tabs:
        tabs = merged_tabs
        active_tab = next(
            (
                dict(tab)
                for tab in tabs
                if str(tab.get("id") or "").strip() == str(active_tab.get("id") or "").strip()
            ),
            dict(tabs[0]),
        )
    meta["tabs"] = tabs
    meta["active_tab_id"] = active_tab.get("id") or ""
    meta["tab_count"] = len(tabs)
    tab_goal = max(1, int(progress.get("tab_goal") or meta.get("tab_goal") or 3))
    refinement_exhausted = bool(progress.get("refinement_exhausted"))
    total_page_count = sum(
        max(int(tab.get("page_count") or 0), 1) for tab in tabs
    )
    meta["tab_goal"] = tab_goal
    meta["tab_target"] = int(progress.get("tab_target") or meta.get("tab_target") or tab_goal)
    meta["pending_tab_count"] = 0 if refinement_exhausted else max(0, tab_goal - len(tabs))
    meta["refinement_exhausted"] = refinement_exhausted
    meta["partial_ready"] = (
        (int(meta.get("pending_tab_count") or 0) > 0 or total_page_count < 2)
        and not refinement_exhausted
    )
    meta["total_page_count"] = sum(
        max(int(tab.get("page_count") or 0), 1) for tab in tabs
    )
    if meta["partial_ready"]:
        meta["loading_label"] = "Refining"
        meta["loading_message"] = (
            "Adding more genre pockets around the strongest tab."
        )
    else:
        meta.pop("loading_label", None)
        meta.pop("loading_message", None)
    updated["meta"] = meta
    updated["items"] = list((active_tab.get("pages") or [active_tab.get("tracks") or []])[0] or [])
    updated["can_extend"] = bool(
        max(int(active_tab.get("page_count") or 0), 1) > 1
    )
    return updated
