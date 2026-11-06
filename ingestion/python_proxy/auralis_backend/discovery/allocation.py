from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Tuple
import hashlib
import json

from .candidates import normalize_text, track_id
from .config import POPULAR_RADIO_CARD_MIN_TRACKS
from .schema import DiscoveryRow, TasteProfile


HISTORY_ROWS = {"last_played", "frequently_listened"}
FAMILIAR_RELATIONS = {
    "direct_history",
}


def _allocation_key(item: Dict[str, Any]) -> str:
    canonical = normalize_text(
        item.get("canonical_entity_id")
        or item.get("canonical_track_identity")
        or item.get("canonical_source_identity")
    )
    if canonical:
        return canonical
    title = normalize_text(item.get("title") or item.get("name"))
    artist = item.get("artist") or item.get("author") or item.get("channel")
    if isinstance(artist, dict):
        artist = artist.get("name") or artist.get("title")
    artist_key = normalize_text(artist)
    if title and artist_key:
        return f"{title}|{artist_key}"
    return track_id(item)


def _history_ids(taste: TasteProfile) -> set[str]:
    return {
        _allocation_key(track)
        for track in (
            taste.recent_tracks
            + taste.top_tracks
            + taste.last_played_tracks
            + taste.anchor_tracks
        )
        if track_id(track)
    }


def _is_discovery(item: Dict[str, Any], history_ids: set[str]) -> bool:
    item_id = _allocation_key(item)
    if item_id and item_id in history_ids:
        return False
    relation = normalize_text(
        item.get("relation_type")
        or item.get("recommendation_relation")
        or item.get("recommendation_path")
    )
    if relation in FAMILIAR_RELATIONS:
        return False
    try:
        novelty = float(item.get("novelty_score") or 0.0)
    except (TypeError, ValueError):
        novelty = 0.0
    # Discovery is recording-level novelty.  An unplayed catalog track by a
    # familiar artist is still discovery; treating the whole artist catalog as
    # familiar made strong, unplayed recommendations look history-dominated.
    return novelty >= 0.5 or relation in {
        "same_artist",
        "same_artist_catalog",
        "same_album",
        "profile_history_anchor",
        "artist_neighbor",
        "artist_graph",
        "track_radio",
        "collaborative_neighbor",
        "trusted_popular_neighbor",
    }


def _balanced_items(
    items: List[Dict[str, Any]],
    *,
    history_ids: set[str],
    global_counts: Counter[str],
    target_count: int,
) -> Tuple[List[Dict[str, Any]], int, int]:
    familiar: List[Dict[str, Any]] = []
    discovery: List[Dict[str, Any]] = []
    for item in items:
        item_id = _allocation_key(item)
        if item_id and global_counts[item_id] >= 1:
            continue
        (discovery if _is_discovery(item, history_ids) else familiar).append(item)

    target_discovery = min(len(discovery), max(int(round(target_count * 0.40)), 1))
    target_familiar = min(len(familiar), max(target_count - target_discovery, 0))
    if target_familiar + target_discovery < target_count:
        extra_discovery = min(len(discovery) - target_discovery, target_count - target_familiar - target_discovery)
        target_discovery += max(extra_discovery, 0)
    if target_familiar + target_discovery < target_count:
        target_familiar += min(len(familiar) - target_familiar, target_count - target_familiar - target_discovery)

    selected: List[Dict[str, Any]] = []
    familiar_index = 0
    discovery_index = 0
    while len(selected) < target_count:
        cycle = len(selected) % 5
        prefer_discovery = cycle in {3, 4}
        source = discovery if prefer_discovery else familiar
        index = discovery_index if prefer_discovery else familiar_index
        limit = target_discovery if prefer_discovery else target_familiar
        if index >= limit:
            source = familiar if prefer_discovery else discovery
            index = familiar_index if prefer_discovery else discovery_index
            limit = target_familiar if prefer_discovery else target_discovery
            if index >= limit:
                break
            prefer_discovery = not prefer_discovery
        item = source[index]
        if prefer_discovery:
            discovery_index += 1
        else:
            familiar_index += 1
        selected.append(item)
        item_id = _allocation_key(item)
        if item_id:
            global_counts[item_id] += 1
    return selected, discovery_index, familiar_index


def allocate_home_rows(
    rows: List[DiscoveryRow],
    taste: TasteProfile,
) -> Tuple[List[DiscoveryRow], Dict[str, Any]]:
    history_ids = _history_ids(taste)
    top_level_counts: Counter[str] = Counter()
    top_level_visible_ids: set[str] = set()
    mix_nested_counts: Counter[str] = Counter()
    radio_nested_counts: Counter[str] = Counter()
    nested_visible_ids: set[str] = set()
    discovery_total = 0
    familiar_total = 0
    row_fingerprints: Dict[str, str] = {}
    row_warnings: Dict[str, List[str]] = {}

    priority = {
        "because_you_played": 0,
        "todays_pick": 1,
        "quiet_picks": 2,
    }
    ordered_rows = sorted(rows, key=lambda row: priority.get(row.kind, 3))
    for row in ordered_rows:
        if row.kind in HISTORY_ROWS:
            continue
        if row.item_type in {"mix", "radio"}:
            nested_counts = radio_nested_counts if row.item_type == "radio" else mix_nested_counts
            nested_discovery = 0
            nested_familiar = 0
            warnings: List[str] = []
            rejected_containers: Dict[str, str] = {}
            updated_containers: List[Dict[str, Any]] = []
            for container in row.items or []:
                nested = container.get("tracks") or container.get("items")
                if not isinstance(nested, list):
                    updated_containers.append(container)
                    continue
                selected: List[Dict[str, Any]] = []
                selected_ids: set[str] = set()
                deferred_visible_overlap: List[Dict[str, Any]] = []
                deep_overlap_count = 0
                deep_overlap_limit = max(1, int(len(nested) * 0.20))
                allow_history = str(container.get("id") or "") == "picked_again"
                container_discovery = 0
                container_familiar = 0
                for track in nested:
                    if not isinstance(track, dict):
                        continue
                    item_id = _allocation_key(track)
                    if not item_id or item_id in selected_ids:
                        continue
                    if (
                        row.item_type == "mix"
                        and not allow_history
                        and item_id in history_ids
                    ):
                        continue
                    if row.item_type == "mix":
                        if len(selected) < 8:
                            if item_id in nested_visible_ids or (
                                not selected and item_id in top_level_visible_ids
                            ):
                                deferred_visible_overlap.append(track)
                                continue
                        elif nested_counts[item_id] > 0:
                            if (
                                nested_counts[item_id] >= 2
                                or deep_overlap_count >= deep_overlap_limit
                            ):
                                continue
                            deep_overlap_count += 1
                    else:
                        if len(selected) < 8:
                            if item_id in top_level_visible_ids or item_id in nested_visible_ids:
                                deferred_visible_overlap.append(track)
                                continue
                        elif nested_counts[item_id] > 0:
                            if (
                                nested_counts[item_id] >= 2
                                or deep_overlap_count >= deep_overlap_limit
                            ):
                                continue
                            deep_overlap_count += 1
                    selected.append(track)
                    selected_ids.add(item_id)
                    nested_counts[item_id] += 1
                    if len(selected) <= 8:
                        nested_visible_ids.add(item_id)
                    if _is_discovery(track, history_ids):
                        container_discovery += 1
                    else:
                        container_familiar += 1
                # These tracks are a second choice for the visible cover area,
                # not rejected supply. Put them back when unique alternatives
                # cannot fill the mix so this soft duplicate preference can
                # never make a valid mix disappear.
                for track in deferred_visible_overlap:
                    item_id = _allocation_key(track)
                    if not item_id or item_id in selected_ids:
                        continue
                    if nested_counts[item_id] > 0:
                        if (
                            nested_counts[item_id] >= 2
                            or deep_overlap_count >= deep_overlap_limit
                        ):
                            continue
                        deep_overlap_count += 1
                    selected.append(track)
                    selected_ids.add(item_id)
                    nested_counts[item_id] += 1
                    if _is_discovery(track, history_ids):
                        container_discovery += 1
                    else:
                        container_familiar += 1
                if row.item_type == "mix" and len(selected) < 8:
                    warnings.append("nested_allocation_below_target")
                    rejected_containers[str(container.get("id") or "mix")] = (
                        f"below_min_tracks:{len(selected)}/8"
                    )
                    for item_id in selected_ids:
                        nested_counts[item_id] -= 1
                        if nested_counts[item_id] <= 0:
                            del nested_counts[item_id]
                    for track in selected[:8]:
                        nested_visible_ids.discard(_allocation_key(track))
                    continue
                if row.item_type == "radio" and len(selected) < POPULAR_RADIO_CARD_MIN_TRACKS:
                    warnings.append("nested_allocation_below_target")
                    rejected_containers[str(container.get("id") or "radio")] = (
                        f"below_min_tracks:{len(selected)}/{POPULAR_RADIO_CARD_MIN_TRACKS}"
                    )
                    for item_id in selected_ids:
                        nested_counts[item_id] -= 1
                        if nested_counts[item_id] <= 0:
                            del nested_counts[item_id]
                    for track in selected[:8]:
                        nested_visible_ids.discard(_allocation_key(track))
                    continue
                if len(selected) < min(len(nested), 8):
                    warnings.append("nested_allocation_below_target")
                nested_discovery += container_discovery
                nested_familiar += container_familiar
                updated_containers.append(
                    {
                        **container,
                        "tracks": selected,
                        "items": selected,
                        "track_count": len(selected),
                    }
                )
            row.items = updated_containers
            discovery_total += nested_discovery
            familiar_total += nested_familiar
            row_warnings[row.kind] = list(dict.fromkeys(warnings))
            row.meta = {
                **dict(row.meta or {}),
                "quality_warnings": list(dict.fromkeys(warnings)),
                "discovery_count": nested_discovery,
                "familiar_count": nested_familiar,
                "rejected_containers": rejected_containers,
            }
            continue
        if row.item_type != "track":
            continue
        row.items = [
            item
            for item in row.items or []
            if not (_allocation_key(item) and _allocation_key(item) in history_ids)
        ]
        original_count = len(row.items or [])
        selected, discovery_count, familiar_count = _balanced_items(
            list(row.items or []),
            history_ids=history_ids,
            global_counts=top_level_counts,
            target_count=original_count,
        )
        row.items = selected
        visible_count = min(
            len(selected),
            max(int((row.meta or {}).get("page_size") or len(selected)), 0),
        )
        top_level_visible_ids.update(
            item_id
            for item in selected[:visible_count]
            if (item_id := _allocation_key(item))
        )
        discovery_total += discovery_count
        familiar_total += familiar_count
        warnings: List[str] = []
        if original_count and len(selected) < min(original_count, 4):
            warnings.append("allocation_below_target")
        row_warnings[row.kind] = warnings
        payload = [track_id(item) or normalize_text(item.get("title")) for item in selected]
        fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        row.meta = {
            **dict(row.meta or {}),
            "quality_warnings": warnings,
            "row_fingerprint": fingerprint,
            "discovery_count": discovery_count,
            "familiar_count": familiar_count,
        }
        row_fingerprints[row.kind] = fingerprint

    mutable_total = discovery_total + familiar_total
    all_counts = Counter(top_level_counts)
    all_counts.update(mix_nested_counts)
    all_counts.update(radio_nested_counts)
    repeated_assignments = sum(max(count - 1, 0) for count in top_level_counts.values())
    return rows, {
        "discovery_ratio": round(discovery_total / max(mutable_total, 1), 4),
        "discovery_count": discovery_total,
        "familiar_count": familiar_total,
        "duplicate_track_count": sum(1 for count in top_level_counts.values() if count > 1),
        "row_overlap_ratio": round(repeated_assignments / max(mutable_total, 1), 4),
        "max_track_row_occurrence": max(top_level_counts.values(), default=0),
        "mix_nested_duplicate_count": sum(1 for count in mix_nested_counts.values() if count > 1),
        "radio_nested_duplicate_count": sum(1 for count in radio_nested_counts.values() if count > 1),
        "cross_partition_duplicate_count": sum(1 for count in all_counts.values() if count > 1),
        "allocation_partitions": {
            "top_level": sum(top_level_counts.values()),
            "made_for_you_nested": sum(mix_nested_counts.values()),
            "popular_radio_nested": sum(radio_nested_counts.values()),
        },
        "row_fingerprints": row_fingerprints,
        "row_warnings": row_warnings,
    }
