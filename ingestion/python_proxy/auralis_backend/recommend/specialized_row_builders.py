from __future__ import annotations

from typing import Any, Callable, Dict, List, Sequence, Tuple


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
) -> Dict[str, Any] | None:
    mix_items: List[Dict[str, Any]] = []
    used_track_ids: set[str] = set()
    blueprints = select_mix_blueprints_fn(
        server,
        profile,
        mix_blueprints_fn(server, profile),
    )
    for index, blueprint in enumerate(blueprints):
        candidates = mix_blueprint_candidates_fn(
            server=server,
            profile=profile,
            snapshot=snapshot,
            blueprint=blueprint,
        )
        if not candidates:
            continue
        tracks = finalize_custom_track_items_fn(
            server=server,
            profile=profile,
            ranking_row_kind=str(blueprint.get("ranking_row_kind") or "trending_for_you"),
            title=str(blueprint.get("label") or title),
            candidates=candidates,
            limit=home_mix_track_cap,
            used_track_ids=used_track_ids,
        )
        if len(tracks) < 5:
            continue
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
        if len(mix_items) >= home_mix_max_count:
            break
    if not mix_items:
        return None
    return {
        "title": title,
        "kind": "mixed_for_you",
        "item_type": "mix",
        "row_style": "mix_cards",
        "items": mix_items,
        "meta": {
            "eyebrow": "Made for this session",
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
) -> Tuple[List[Dict[str, Any]], Dict[str, Any] | None]:
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
    tabs: List[Dict[str, Any]] = []
    used_track_ids: set[str] = set()
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
        if len(tracks) < 4:
            continue
        diversified_tracks: List[Dict[str, Any]] = []
        local_artist_keys: set[str] = set()
        selected_signatures: set[str] = set()
        for track in tracks:
            track_signature = server._recommendation_track_signature(track)
            artist_key = server._normalize_text(
                track.get("channel") or track.get("artist") or track.get("author") or ""
            )
            if track_signature and track_signature in used_track_ids:
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
        if len(diversified_tracks) < min(4, home_genre_track_cap):
            for track in tracks:
                track_signature = server._recommendation_track_signature(track)
                if track_signature and track_signature in selected_signatures:
                    continue
                if track_signature and track_signature in used_track_ids:
                    continue
                diversified_tracks.append(track)
                if track_signature:
                    selected_signatures.add(track_signature)
                if len(diversified_tracks) >= home_genre_track_cap:
                    break
        if len(diversified_tracks) < 4:
            continue
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
        if len(tabs) >= home_genre_tab_limit:
            break
    if len(tabs) < 2:
        return [], None
    active_tab = next(
        (
            dict(tab)
            for tab in tabs
            if str(tab.get("id") or "").strip() == genre_tab_identifier_fn(selected_tab_id)
        ),
        dict(tabs[0]),
    )
    return tabs, active_tab


def build_trending_by_genre_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
    trending_genre_tabs_fn: Callable[..., Tuple[List[Dict[str, Any]], Dict[str, Any] | None]],
) -> Dict[str, Any] | None:
    tabs, active_tab = trending_genre_tabs_fn(
        server=server,
        profile=profile,
        snapshot=snapshot,
    )
    if len(tabs) < 2 or not isinstance(active_tab, dict):
        return None
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
    preferred_ids = [
        str(tab.get("id") or "").strip()
        for tab in list(meta.get("tabs") or [])
        if isinstance(tab, dict) and str(tab.get("id") or "").strip()
    ]
    tabs, active_tab = trending_genre_tabs_fn(
        preferred_tab_ids=preferred_ids,
        selected_tab_id=tab_id,
    )
    if len(tabs) < 2 or not isinstance(active_tab, dict):
        updated["can_extend"] = False
        return updated
    meta["tabs"] = tabs
    meta["active_tab_id"] = active_tab.get("id") or ""
    updated["meta"] = meta
    updated["items"] = list((active_tab.get("pages") or [active_tab.get("tracks") or []])[0] or [])
    updated["can_extend"] = False
    return updated
