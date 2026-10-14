from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence
import hashlib
import json
import time
import uuid

from ..discovery.artifact import (
    artifact_to_dict,
    build_diagnostics,
    evaluate_quality,
    _artifact_from_dict,
)
from ..discovery.config import (
    ENGINE_MODEL_VERSION,
    FEED_PROMOTION_CONTRACT_VERSION,
    ROW_ORDER,
    ROW_RECIPES,
)
from ..discovery.inventory import (
    CandidateInventory,
    candidate_inventory_from_payload,
    canonical_item_identity,
)
from ..discovery.radio_inventory import (
    ArtistRadioInventory,
    artist_radio_inventory_from_payload,
    radio_card_candidates,
    select_radio_rotation,
)
from ..discovery.ranking import admitted_artist_candidates, build_rows_from_pools
from ..discovery.schema import DiscoveryArtifact, DiscoveryRow
from ..search.intelligence import load_catalog_artist_records, normalize_artist_name
from ..storage.artist_artwork import attach_persisted_artist_artwork
from .contracts import (
    ACTIVE_ARTIFACT_TTL_SECONDS,
    HOME_DISCOVERY_EXPOSURE_COOLDOWN_SECONDS,
    HOME_ROW_CONTRACT_BY_KIND,
    QUEUE_TARGET_DEPTH,
    FeedArtifactRecord,
    artist_identity_aliases,
    evaluate_feed_contract,
    evaluate_popular_radio_contract,
)
from .bundles import with_feed_bundle_manifest
from .repositories import (
    HomeRepositorySet,
    HomeRepositoryConflict,
    ImmutableRecordConflict,
    QueueFull,
    StaleRevision,
)


_OPTIONAL_ROW_KINDS = {
    "recommended_albums",
}

_MUTABLE_SUCCESSOR_ROWS = {
    "todays_pick",
    "featured_new_albums",
    "made_for_you",
    "because_you_played",
    "popular_radio",
    "recommended_albums",
    "recommended_artists",
    "quiet_picks",
}
FEATURED_ALBUM_MIN_NEW_ARTISTS = 3


def _earned_row_kinds(taste: Any, rows: Sequence[Any] | None = None) -> set[str]:
    """Return earned rows the row builder is actually eligible to emit.

    History rows have explicit minimum-history gates in ``build_rows_from_pools``.
    Treating any non-empty history as earned makes a 1-7 track history require
    an 8-track row that the builder deliberately omits. Other rows are gated by
    anchor existence and remain required when any anchor exists; their output
    quality is still enforced by the feed contract.
    """

    earned: set[str] = set()
    row_counts = {
        str(getattr(row, "kind", "")): len(getattr(row, "items", []) or [])
        for row in rows or []
    }
    for kind, evidence in (
        ("last_played", getattr(taste, "last_played_tracks", [])),
        ("frequently_listened", getattr(taste, "frequent_tracks", [])),
    ):
        minimum = HOME_ROW_CONTRACT_BY_KIND[kind].min_items
        eligible_count = row_counts.get(kind, 0) if rows is not None else len(evidence or [])
        if eligible_count >= minimum:
            earned.add(kind)
    if (
        getattr(taste, "full_history_tracks", [])
        or getattr(taste, "recent_tracks", [])
        or getattr(taste, "anchor_tracks", [])
    ):
        earned.add("because_you_played")
    return earned


def _stable_artist_ids(value: Mapping[str, Any]) -> set[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    canonical = str(value.get("canonical_artist_id") or "").strip()
    if canonical.casefold().startswith("musicbrainz:artist:"):
        identities.add(("musicbrainz", canonical.removeprefix("musicbrainz:artist:").casefold()))
    elif canonical.casefold().startswith("provider:artist:"):
        identities.add(("provider", canonical.removeprefix("provider:artist:")))
    for key in ("musicbrainz_artist_id", "artist_mbid", "mb_artist_id"):
        raw = str(value.get(key) or "").strip().casefold().removeprefix("musicbrainz:artist:")
        if raw:
            identities.add(("musicbrainz", raw))
    for key in ("provider_artist_id", "artist_id", "artistId", "browseId"):
        raw = str(value.get(key) or "").strip()
        if raw.casefold().startswith("provider:artist:"):
            raw = raw.removeprefix("provider:artist:")
        if raw:
            identities.add(("provider", raw))
    raw_id = str(value.get("id") or "").strip()
    if raw_id.casefold().startswith("musicbrainz:artist:"):
        identities.add(("musicbrainz", raw_id.removeprefix("musicbrainz:artist:").casefold()))
    elif raw_id.casefold().startswith("provider:artist:"):
        identities.add(("provider", raw_id.removeprefix("provider:artist:")))
    elif raw_id and not raw_id.casefold().startswith(("artist-name:", "mix:")):
        identities.add(("provider", raw_id))
    return identities


def candidate_artist_artwork_repairs(
    candidate: CandidateInventory,
    server: Any,
    *,
    limit: int = 8,
    taste: Any = None,
) -> list[dict[str, Any]]:
    """Return bounded artist-only candidates without verified persisted art."""
    artists: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    pools = ({"admitted": list(admitted_artist_candidates(candidate.pools, taste))}
             if taste is not None else candidate.pools)
    for pool in (pools or {}).values():
        for entry in pool or []:
            if getattr(entry, "item_type", "track") != "artist":
                continue
            item = dict(getattr(entry, "item", {}) or {})
            name = str(item.get("name") or item.get("artist") or "").strip()
            stable_ids = _stable_artist_ids(item)
            if str(item.get("id") or "") == normalize_artist_name(name) and not any(
                item.get(field) for field in ("provider_artist_id", "musicbrainz_artist_id", "canonical_artist_id", "artist_id")
            ):
                stable_ids = set()
            if not name or not stable_ids:
                continue
            identity = tuple(sorted(stable_ids))
            if identity in seen:
                continue
            seen.add(identity)
            if not item.get("provider_artist_id"):
                provider_id = next((value for kind, value in stable_ids if kind == "provider"), "")
                if provider_id:
                    item["provider_artist_id"] = provider_id
            item["name"] = name
            artists.append(item)
            if len(artists) >= max(0, limit) * 3:
                break
        if len(artists) >= max(0, limit) * 3:
            break
    if not artists or limit <= 0:
        return []

    persisted = load_catalog_artist_records(
        server,
        artist_names=[str(item.get("name") or "") for item in artists],
    )
    repairs: list[dict[str, Any]] = []
    for item in artists:
        name_key = normalize_artist_name(item.get("name"))
        record = persisted.get(name_key) or {}
        item_ids = _stable_artist_ids(item)
        record_ids = _stable_artist_ids(record)
        matching_record = record if item_ids & record_ids else {}
        verified_candidate = attach_persisted_artist_artwork(server, dict(item))
        verified_record = attach_persisted_artist_artwork(server, dict(matching_record))
        if str(verified_candidate.get("thumbnail") or "").startswith("/artist_artwork/"):
            continue
        if str(verified_record.get("thumbnail") or "").startswith("/artist_artwork/"):
            continue
        repairs.append(item)
        if len(repairs) >= limit:
            break
    return repairs


def _attach_inventory_portraits(
    rows: Sequence[DiscoveryRow],
    radio: ArtistRadioInventory | None,
    server: Any = None,
) -> None:
    """Reuse artist-owned portraits from persisted radio supply; never fetch."""
    portraits: dict[tuple[str, str], set[str]] = {}
    for card in (radio.reservoir_cards or radio.cards) if radio is not None else []:
        thumbnail = str(card.get('thumbnail') or '')
        owner = str(card.get('artwork_owner_identity') or '')
        if (card.get('radio_mode') != 'artist_inventory'
                or not thumbnail.startswith('/artist_artwork/')
                or not owner or owner != card.get('seed_artist_identity')):
            continue
        for identity in _stable_artist_ids({"id": owner}):
            portraits.setdefault(identity, set()).add(thumbnail)
    artist_items = [
        item for row in rows if row.item_type == "artist"
        for item in row.items
    ]
    persisted = load_catalog_artist_records(
        server,
        artist_names=[str(item.get("name") or item.get("artist") or "") for item in artist_items],
    ) if server is not None else {}
    for row in rows:
        if row.item_type != 'artist':
            continue
        for item in row.items:
            item_ids = _stable_artist_ids(item)
            if server is not None and item_ids:
                name_key = normalize_artist_name(item.get("name") or item.get("artist"))
                record = persisted.get(name_key) or {}
                if item_ids & _stable_artist_ids(record):
                    verified = attach_persisted_artist_artwork(server, dict(record))
                    thumbnail = str(verified.get("thumbnail") or "")
                    if thumbnail.startswith("/artist_artwork/"):
                        item.update({key: value for key, value in verified.items()
                                     if key.startswith("artwork_") or key in
                                     {"thumbnail", "canonical_artist_id", "musicbrainz_artist_id"}})
                        continue
            if item.get('thumbnail'):
                continue
            matches = set().union(*(portraits.get(identity, set()) for identity in item_ids)) if item_ids else set()
            if len(matches) == 1:
                item['thumbnail'] = next(iter(matches))


@dataclass(frozen=True)
class ComposeOutcome:
    stored: bool = False
    initialized_active: bool = False
    artifact_id: str = ""
    candidate_snapshot_id: str = ""
    radio_snapshot_id: str = ""
    failed_contracts: tuple[str, ...] = ()
    reason: str = ""


def artifact_from_record(record: FeedArtifactRecord | None) -> DiscoveryArtifact | None:
    if record is None:
        return None
    return _artifact_from_dict(dict(record.payload or {}))


def artifact_exposure_entries(
    artifact: DiscoveryArtifact,
    *,
    visible_only: bool = False,
    row_offset: int = 0,
    row_limit: int | None = None,
) -> set[tuple[str, str, str]]:
    entries: set[tuple[str, str, str]] = set()
    rows = list(artifact.rows or [])[max(int(row_offset or 0), 0):]
    if row_limit is not None:
        rows = rows[:max(int(row_limit or 0), 0)]
    for row in rows:
        items = list(row.items or [])
        if visible_only:
            recipe = ROW_RECIPES.get(row.kind)
            items = items[: max(int(getattr(recipe, "page_size", 0) or 8), 1)]
        for item in items:
            if not isinstance(item, Mapping):
                continue
            item_kind = str(row.item_type or "track").strip() or "track"
            raw_id = str(item.get("id") or item.get("videoId") or "").strip()
            canonical = canonical_item_identity(dict(item), item_type=item_kind)
            for identity in (raw_id, canonical):
                if identity:
                    entries.add((row.kind, item_kind, identity.casefold()))
            if item_kind == "album":
                artist = " ".join(str(item.get("artist") or "").strip().casefold().split())
                title = " ".join(
                    str(item.get("title") or item.get("album") or "")
                    .strip().casefold().split()
                )
                release_group = str(
                    item.get("musicbrainz_release_group_id")
                    or item.get("release_group_mbid")
                    or ""
                ).strip().casefold()
                if title:
                    entries.add((row.kind, item_kind, f"{title}|{artist}"))
                if release_group:
                    entries.add((row.kind, item_kind, f"release-group:{release_group}"))
                # Album identity alone allowed a successor to replace every
                # album while retaining the exact same artist shelf.  Keep a
                # separate artist exposure so Featured Albums can rank unseen
                # artists first and use familiar artists only as backfill.
                if artist:
                    entries.add((row.kind, "artist", f"artist:{artist}"))
            if item_kind == "artist":
                name = " ".join(
                    str(item.get("name") or item.get("artist") or "")
                    .strip().casefold().split()
                )
                if name:
                    entries.add((row.kind, item_kind, name))
            if row.kind == "popular_radio":
                artist_name = " ".join(
                    str(item.get("artist_name") or "")
                    .strip()
                    .casefold()
                    .split()
                )
                if artist_name:
                    entries.add((row.kind, "artist", f"artist:{artist_name}"))
                for artist_identity in (
                    item.get("seed_artist_identity"),
                    item.get("seed_artist_key"),
                    *(item.get("seed_artist_identities") or []),
                ):
                    normalized_artist = " ".join(
                        str(artist_identity or "").strip().casefold().split()
                    )
                    if normalized_artist:
                        entries.add((row.kind, "artist", normalized_artist))
            for nested in list(item.get("tracks") or item.get("items") or [])[:24]:
                if not isinstance(nested, Mapping):
                    continue
                nested_id = str(
                    nested.get("id") or nested.get("videoId") or ""
                ).strip()
                nested_canonical = canonical_item_identity(
                    dict(nested), item_type="track"
                )
                for identity in (nested_id, nested_canonical):
                    if identity:
                        entries.add((row.kind, "track", identity.casefold()))
    return entries


def _visible_track_ids(artifacts: Iterable[DiscoveryArtifact]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for artifact in artifacts:
        for _row_kind, _item_kind, identity in artifact_exposure_entries(
            artifact,
            visible_only=True,
        ):
            if identity and identity not in seen:
                seen.add(identity)
                output.append(identity)
    return output


def _radio_exclusions(
    artifacts: Sequence[DiscoveryArtifact],
) -> tuple[set[str], set[str], set[str]]:
    card_ids: set[str] = set()
    track_ids: set[str] = set()
    comparison: set[str] = set()
    for artifact_index, artifact in enumerate(artifacts):
        row_cards: set[str] = set()
        for row in artifact.rows or []:
            if row.kind != "popular_radio":
                continue
            for card in row.items or []:
                if not isinstance(card, Mapping):
                    continue
                card_id = str(card.get("id") or "").strip()
                if card_id:
                    card_ids.add(card_id)
                    row_cards.add(card_id)
                for track in card.get("tracks") or card.get("items") or []:
                    if isinstance(track, Mapping):
                        identity = canonical_item_identity(dict(track), item_type="track")
                        if identity:
                            track_ids.add(identity)
        if artifact_index == len(artifacts) - 1:
            comparison = row_cards
    return card_ids, track_ids, comparison


def _preserve_complete_optional_rows(
    rows: list[DiscoveryRow],
    parent: DiscoveryArtifact | None,
) -> tuple[list[DiscoveryRow], list[str]]:
    if parent is None:
        return rows, []
    output = list(rows)
    positions = {row.kind: index for index, row in enumerate(output)}
    preserved: list[str] = []
    for row in parent.rows or []:
        if row.kind not in _OPTIONAL_ROW_KINDS:
            continue
        recipe = ROW_RECIPES.get(row.kind)
        if recipe is None or len(row.items or []) < recipe.min_items:
            continue
        if (
            row.kind == "popular_radio"
            and not evaluate_popular_radio_contract(row.items or []).accepted
        ):
            continue
        existing_index = positions.get(row.kind)
        existing = output[existing_index] if existing_index is not None else None
        existing_complete = bool(
            existing is not None
            and len(existing.items or []) >= recipe.min_items
            and (
                row.kind != "popular_radio"
                or evaluate_popular_radio_contract(existing.items or []).accepted
            )
        )
        if existing_complete:
            continue
        if existing_index is None:
            positions[row.kind] = len(output)
            output.append(row)
        else:
            output[existing_index] = row
        preserved.append(row.kind)
    order = {kind: index for index, kind in enumerate(ROW_ORDER)}
    output.sort(key=lambda row: order.get(row.kind, len(order)))
    return output, preserved


def _new_row_artist_count(
    current: DiscoveryRow | None,
    previous: DiscoveryRow | None,
) -> int:
    if current is None:
        return 0
    prior_aliases: set[str] = set()
    for item in (previous.items if previous is not None else []) or []:
        if isinstance(item, Mapping):
            prior_aliases.update(artist_identity_aliases(item))
    count = 0
    seen: set[str] = set()
    for item in current.items or []:
        if not isinstance(item, Mapping):
            continue
        aliases = artist_identity_aliases(item)
        identity = next(iter(sorted(aliases)), "")
        if not identity or aliases & seen:
            continue
        seen.update(aliases)
        if not (aliases & prior_aliases):
            count += 1
    return count


def _content_signature(artifact: DiscoveryArtifact) -> str:
    payload = [
        {
            "kind": row.kind,
            "items": [
                str(item.get("id") or item.get("videoId") or "")
                for item in row.items or []
                if isinstance(item, Mapping)
            ],
        }
        for row in artifact.rows or []
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _successor_item_identity(kind: str, item: Mapping[str, Any]) -> str:
    if kind == "popular_radio":
        return str(
            item.get("seed_artist_identity")
            or item.get("seed_artist_key")
            or item.get("id")
            or ""
        ).strip().casefold()
    return str(item.get("id") or item.get("videoId") or "").strip().casefold()


def _meaningfully_differs(
    previous: DiscoveryArtifact,
    candidate: DiscoveryArtifact,
) -> bool:
    """Require visible novelty, not changes hidden in pageable reserves."""

    return bool(_successor_novelty(previous, candidate)["accepted"])


def _successor_novelty(
    previous: DiscoveryArtifact,
    candidate: DiscoveryArtifact,
) -> dict[str, Any]:
    """Describe the user-visible difference between two feed artifacts."""

    previous_rows = {row.kind: row for row in previous.rows or []}
    candidate_rows = {row.kind: row for row in candidate.rows or []}
    changed_rows = 0
    comparable_rows = 0
    total_new = 0
    total_after = 0
    rows: dict[str, dict[str, int | bool]] = {}
    for kind in _MUTABLE_SUCCESSOR_ROWS:
        before = previous_rows.get(kind)
        after = candidate_rows.get(kind)
        if before is None or after is None:
            if before is not after:
                changed_rows += 1
                after_count = len(after.items or []) if after is not None else 0
                total_new += after_count
                total_after += after_count
                rows[kind] = {
                    "before": len(before.items or []) if before is not None else 0,
                    "after": after_count,
                    "new": after_count,
                    "changed": True,
                }
            continue
        recipe = ROW_RECIPES.get(kind)
        visible_limit = max(int(getattr(recipe, "page_size", 0) or 8), 1)
        before_ids = {
            identity
            for item in list(before.items or [])[:visible_limit]
            if isinstance(item, Mapping)
            and (identity := _successor_item_identity(kind, item))
        }
        after_ids = {
            identity
            for item in list(after.items or [])[:visible_limit]
            if isinstance(item, Mapping)
            and (identity := _successor_item_identity(kind, item))
        }
        if not before_ids and not after_ids:
            continue
        comparable_rows += 1
        new_count = len(after_ids - before_ids)
        minimum_new = max(2, (len(after_ids) + 3) // 4)
        changed = new_count >= minimum_new
        if changed:
            changed_rows += 1
        total_new += new_count
        total_after += len(after_ids)
        rows[kind] = {
            "before": len(before_ids),
            "after": len(after_ids),
            "new": new_count,
            "changed": changed,
        }
    # Two visibly changed shelves are enough. A broader global change also
    # qualifies when it is distributed across several shelves below their
    # individual 25% threshold. This avoids rejecting useful local rotations
    # while still refusing a mere reorder of identical items.
    global_minimum = max(8, (total_after + 5) // 6)
    accepted = bool(
        changed_rows >= min(2, comparable_rows)
        or total_new >= global_minimum
    )
    return {
        "accepted": accepted,
        "changed_rows": changed_rows,
        "comparable_rows": comparable_rows,
        "new_visible_identities": total_new,
        "visible_identity_count": total_after,
        "required_global_new": global_minimum,
        "rows": rows,
    }


def compose_snapshot_artifact(
    *,
    taste: Any,
    candidate: CandidateInventory,
    radio: ArtistRadioInventory | None,
    prior_artifacts: Sequence[DiscoveryArtifact],
    exposure_ids: Iterable[str] = (),
    rotation_epoch: int,
    reason: str,
    server: Any = None,
    now: float | None = None,
) -> DiscoveryArtifact:
    """Compose an immutable artifact, enriching artist art from local records."""

    started = time.perf_counter()
    generated_at = time.time() if now is None else float(now)
    exposure_values = {
        str(value or "").strip().casefold()
        for value in exposure_ids
        if str(value or "").strip()
    }
    avoid_ids = list(
        dict.fromkeys(
            [
                *list(getattr(taste, "avoid_ids", []) or []),
                *_visible_track_ids(prior_artifacts),
                *exposure_values,
            ]
        )
    )
    hard_avoid_ids = _visible_track_ids(prior_artifacts)
    rotation_taste = replace(
        taste,
        avoid_ids=avoid_ids,
        hard_avoid_ids=hard_avoid_ids,
        force_refresh=True,
        rotation_epoch=max(int(rotation_epoch or 0), 0),
        refresh_token=(
            f"{taste.user_scope_id}:{taste.profile_key}:"
            f"{candidate.generation_id}:{max(int(rotation_epoch or 0), 0)}:{reason}"
        ),
    )
    pools = {name: list(values or []) for name, values in candidate.pools.items()}
    parent = prior_artifacts[-1] if prior_artifacts else None
    independent_warnings: list[str] = []
    selected_radio: ArtistRadioInventory | None = None
    if radio is not None:
        # Discovery is measured against actual taste evidence and the radio's
        # own durable exposure ledger. Artists shown by unrelated Home rows are
        # not proof that the user knows them.
        known_radio_artists: set[str] = set()
        for value in (
            *list(getattr(taste, "listened_artists", []) or []),
            *list(getattr(taste, "top_artists", []) or []),
            *list(getattr(taste, "artist_hints", []) or []),
        ):
            known_radio_artists.update(artist_identity_aliases(str(value or "")))
        for artifact in prior_artifacts:
            for row in artifact.rows or []:
                if row.kind != "popular_radio":
                    continue
                for card in row.items or []:
                    if isinstance(card, Mapping):
                        known_radio_artists.update(artist_identity_aliases(card))
        card_ids, track_ids, comparison = _radio_exclusions(prior_artifacts)
        selected_radio = select_radio_rotation(
            radio,
            excluded_card_ids=card_ids,
            excluded_track_ids=track_ids,
            comparison_card_ids=comparison,
            known_artist_identities=known_radio_artists,
            epoch=max(int(rotation_epoch or 0), 0),
        )
        if selected_radio is not None and selected_radio.is_ready:
            cards = list(selected_radio.cards or [])
            previous_radio_artists = {
                str(
                    card.get("seed_artist_identity")
                    or card.get("seed_artist_key")
                    or card.get("id")
                    or ""
                ).strip().casefold()
                for artifact in prior_artifacts[-1:]
                for row in artifact.rows or []
                if row.kind == "popular_radio"
                for card in row.items or []
                if isinstance(card, Mapping)
            }
            base_radio_report = evaluate_popular_radio_contract(
                cards,
                known_artist_identities=known_radio_artists,
            )
            radio_report = evaluate_popular_radio_contract(
                cards,
                previous_artist_identities=previous_radio_artists,
                known_artist_identities=known_radio_artists,
            )
            if base_radio_report.accepted:
                pools["popular_radio_cards"] = radio_card_candidates(selected_radio)
                if parent is not None and not radio_report.accepted:
                    independent_warnings.extend(radio_report.violations)
            elif parent is not None:
                independent_warnings.extend(base_radio_report.violations)
    if (
        parent is not None
        and any(row.kind == "popular_radio" for row in parent.rows or [])
        and "popular_radio_cards" not in pools
        and not any("radio_" in value for value in independent_warnings)
    ):
        independent_warnings.append("popular_radio_replacement_unavailable")

    rows, row_status, lanes, lane_diagnostics = build_rows_from_pools(
        pools,
        rotation_taste,
    )
    _attach_inventory_portraits(rows, radio, server)
    # A history list can contain repeated or unplayable records. Only publish
    # the earned row when the builder produced its contracted eligible count.
    rows = [
        row for row in rows
        if row.kind not in {"last_played", "frequently_listened"}
        or len(row.items or []) >= HOME_ROW_CONTRACT_BY_KIND[row.kind].min_items
    ]
    if parent is not None:
        current_featured = next(
            (row for row in rows if row.kind == "featured_new_albums"),
            None,
        )
        previous_featured = next(
            (row for row in parent.rows or [] if row.kind == "featured_new_albums"),
            None,
        )
        if previous_featured is not None:
            new_artist_count = _new_row_artist_count(
                current_featured,
                previous_featured,
            )
            if new_artist_count < FEATURED_ALBUM_MIN_NEW_ARTISTS:
                independent_warnings.append(
                    "featured_new_albums_artist_novelty:"
                    f"{new_artist_count}/{FEATURED_ALBUM_MIN_NEW_ARTISTS}"
                )
    rows, preserved = _preserve_complete_optional_rows(rows, parent)
    for kind in preserved:
        row = next(value for value in rows if value.kind == kind)
        row_status[kind] = {
            "status": "emitted",
            "count": len(row.items or []),
            "warnings": ["preserved_parent_until_replacement"],
        }
    accepted, quality_reasons, artifact_quality = evaluate_quality(
        rows=rows,
        taste=rotation_taste,
        home_tab_diagnostics=lane_diagnostics,
    )
    earned = _earned_row_kinds(rotation_taste, rows)
    final_contract = evaluate_feed_contract(
        [
            {"kind": row.kind, "items": list(row.items or [])}
            for row in rows
        ],
        earned_row_kinds=earned,
    )
    if not final_contract.accepted:
        accepted = False
        quality_reasons = [*quality_reasons, *final_contract.violations]
        artifact_quality = "rejected"
    candidate_counts = dict(candidate.candidate_counts or {})
    candidate_counts["popular_radio_cards"] = len(
        pools.get("popular_radio_cards") or []
    )
    diagnostics = build_diagnostics(
        artifact_source="home_snapshot_v2",
        artifact_quality=artifact_quality,
        row_status=row_status,
        rows=rows,
        candidate_pool_counts=candidate_counts,
        provider_timings_ms=dict(candidate.provider_timings_ms or {}),
        home_tab_lanes=lanes,
        home_tab_diagnostics=lane_diagnostics,
        quality_reasons=quality_reasons,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        taste=rotation_taste,
    )
    diagnostics.update(
        {
            "model_version": ENGINE_MODEL_VERSION,
            "feed_promotion_contract": FEED_PROMOTION_CONTRACT_VERSION,
            "candidate_snapshot_id": candidate.generation_id,
            "radio_snapshot_id": (
                selected_radio.generation_id if selected_radio is not None else ""
            ),
            "inventory_generation_id": candidate.generation_id,
            "rotation_epoch": max(int(rotation_epoch or 0), 0),
            "feed_external_calls": 0,
            "provider_network_work_count": 0,
            "preserved_optional_rows": preserved,
            "independent_supply_warnings": list(
                dict.fromkeys(independent_warnings)
            ),
            "composition_reason": reason,
            "exposure_cooldown_identity_count": len(exposure_values),
            "home_repository_cutover": True,
        }
    )
    if parent is not None:
        preview = DiscoveryArtifact(
            session_id="novelty-preview",
            user_scope_id=taste.user_scope_id,
            profile_key=taste.profile_key,
            generated_at=generated_at,
            expires_at=generated_at + ACTIVE_ARTIFACT_TTL_SECONDS,
            rows=rows,
            diagnostics={},
            candidate_pool_counts={},
            provider_timings_ms={},
            home_tab_lanes={},
            accepted=accepted,
        )
        diagnostics["successor_novelty"] = _successor_novelty(parent, preview)
    return DiscoveryArtifact(
        session_id=str(uuid.uuid4()),
        user_scope_id=taste.user_scope_id,
        profile_key=taste.profile_key,
        generated_at=generated_at,
        expires_at=generated_at + ACTIVE_ARTIFACT_TTL_SECONDS,
        rows=rows,
        diagnostics=diagnostics,
        candidate_pool_counts=candidate_counts,
        provider_timings_ms=dict(candidate.provider_timings_ms or {}),
        home_tab_lanes=lanes,
        accepted=accepted,
        quality_reasons=list(dict.fromkeys(quality_reasons)),
        artifact_source="home_snapshot_v2",
    )


class HomeFeedComposer:
    """Load immutable inputs, call the pure composer, and commit via CAS."""

    def __init__(self, repositories: HomeRepositorySet) -> None:
        self._repositories = repositories

    def _attach_materialized(
        self,
        record: FeedArtifactRecord,
        *,
        state: Any,
    ) -> ComposeOutcome:
        if record.artifact_id == state.active_artifact_id or record.artifact_id in set(
            state.ready_artifact_ids
        ):
            return ComposeOutcome(
                candidate_snapshot_id=record.candidate_snapshot_id,
                radio_snapshot_id=record.radio_snapshot_id,
                reason="supply_pair_consumed",
            )
        try:
            if not state.active_artifact_id:
                self._repositories.state.initialize_active(
                    record.user_scope_id,
                    record.artifact_id,
                    expected_revision=state.revision,
                )
                return ComposeOutcome(
                    stored=True,
                    initialized_active=True,
                    artifact_id=record.artifact_id,
                    candidate_snapshot_id=record.candidate_snapshot_id,
                    radio_snapshot_id=record.radio_snapshot_id,
                    reason="existing_supply_initialized",
                )
            self._repositories.state.append_ready(
                record.user_scope_id,
                record.artifact_id,
                expected_revision=state.revision,
            )
            return ComposeOutcome(
                stored=True,
                artifact_id=record.artifact_id,
                candidate_snapshot_id=record.candidate_snapshot_id,
                radio_snapshot_id=record.radio_snapshot_id,
                reason="existing_supply_attached",
            )
        except QueueFull:
            return ComposeOutcome(
                candidate_snapshot_id=record.candidate_snapshot_id,
                radio_snapshot_id=record.radio_snapshot_id,
                reason="queue_full",
            )
        except (StaleRevision, HomeRepositoryConflict):
            return ComposeOutcome(
                candidate_snapshot_id=record.candidate_snapshot_id,
                radio_snapshot_id=record.radio_snapshot_id,
                reason="state_commit_conflict",
            )

    def compose(
        self,
        taste: Any,
        *,
        reason: str,
        candidate_snapshot_id: str = "",
        radio_snapshot_id: str = "",
        allocation_epoch: int = 0,
        server: Any = None,
    ) -> ComposeOutcome:
        scope = str(taste.user_scope_id or "guest")
        state = self._repositories.state.reconcile(scope)
        if len(state.ready_artifact_ids) >= QUEUE_TARGET_DEPTH:
            return ComposeOutcome(reason="queue_full")
        epoch = max(int(allocation_epoch or 0), 0)
        if epoch <= 0:
            epoch = state.active_version + len(state.ready_artifact_ids) + 1
        candidate_record = (
            self._repositories.candidates.get(candidate_snapshot_id)
            if candidate_snapshot_id
            else (
                self._repositories.candidates.latest_fresh(
                    scope,
                    profile_fingerprint=str(taste.profile_key or ""),
                )
                or self._repositories.candidates.latest_usable(
                    scope,
                    profile_fingerprint=str(taste.profile_key or ""),
                )
            )
        )
        if (
            candidate_record is None
            or candidate_record.user_scope_id != scope
            or not candidate_record.is_usable()
        ):
            return ComposeOutcome(reason="candidate_snapshot_missing")
        candidate = candidate_inventory_from_payload(dict(candidate_record.payload))
        if candidate is None or not candidate.is_ready:
            return ComposeOutcome(reason="candidate_snapshot_invalid")
        composition_taste = replace(
            taste,
            profile_key=candidate_record.profile_fingerprint,
        )
        radio_record = (
            self._repositories.radio.get(radio_snapshot_id)
            if radio_snapshot_id
            else self._repositories.radio.latest_usable(
                scope,
                profile_fingerprint=candidate_record.profile_fingerprint,
            )
        )
        if radio_record is None and not radio_snapshot_id:
            # One-time compatibility path for reservoirs written before the
            # stable Home supply fingerprint existed.
            radio_record = self._repositories.radio.latest_usable(scope)
        if radio_record is not None and radio_record.user_scope_id != scope:
            radio_record = None
        radio = (
            artist_radio_inventory_from_payload(dict(radio_record.payload))
            if radio_record is not None
            else None
        )
        if radio is not None and not radio.is_ready:
            radio = None
            radio_record = None
        existing_pair = self._repositories.artifacts.get_by_supply(
            scope,
            candidate_record.snapshot_id,
            radio_record.snapshot_id if radio_record else "",
            allocation_epoch=epoch,
        )
        if existing_pair is not None:
            return self._attach_materialized(existing_pair, state=state)
        prior_records = [
            self._repositories.artifacts.get(artifact_id)
            for artifact_id in (
                [state.active_artifact_id] if state.active_artifact_id else []
            )
            + list(state.ready_artifact_ids)
        ]
        prior_artifacts = [
            artifact
            for artifact in (artifact_from_record(record) for record in prior_records)
            if artifact is not None
        ]
        artifact = compose_snapshot_artifact(
            taste=composition_taste,
            candidate=candidate,
            radio=radio,
            prior_artifacts=prior_artifacts,
            exposure_ids=self._repositories.exposures.recent_identities(
                scope,
                since=time.time() - HOME_DISCOVERY_EXPOSURE_COOLDOWN_SECONDS,
            ),
            rotation_epoch=epoch,
            reason=reason,
            server=server,
        )
        if not artifact.accepted:
            return ComposeOutcome(
                candidate_snapshot_id=candidate_record.snapshot_id,
                radio_snapshot_id=radio_record.snapshot_id if radio_record else "",
                failed_contracts=tuple(artifact.quality_reasons or ()),
                reason="artifact_contract_rejected",
            )
        if prior_artifacts and _content_signature(prior_artifacts[-1]) == _content_signature(
            artifact
        ):
            return ComposeOutcome(
                candidate_snapshot_id=candidate_record.snapshot_id,
                radio_snapshot_id=radio_record.snapshot_id if radio_record else "",
                reason="artifact_unchanged",
            )
        if prior_artifacts and not _meaningfully_differs(prior_artifacts[-1], artifact):
            novelty = _successor_novelty(prior_artifacts[-1], artifact)
            return ComposeOutcome(
                candidate_snapshot_id=candidate_record.snapshot_id,
                radio_snapshot_id=radio_record.snapshot_id if radio_record else "",
                failed_contracts=(
                    "successor_visible_novelty:"
                    f"{novelty['new_visible_identities']}/"
                    f"{novelty['required_global_new']}:"
                    f"rows={novelty['changed_rows']}",
                ),
                reason="artifact_too_similar",
            )
        artifact_payload = with_feed_bundle_manifest(artifact_to_dict(artifact))
        artifact.diagnostics = dict(artifact_payload.get("diagnostics") or {})
        record = FeedArtifactRecord(
            artifact_id=artifact.session_id,
            user_scope_id=scope,
            profile_fingerprint=composition_taste.profile_key,
            candidate_snapshot_id=candidate_record.snapshot_id,
            radio_snapshot_id=radio_record.snapshot_id if radio_record else "",
            allocation_epoch=epoch,
            created_at=artifact.generated_at,
            expires_at=artifact.expires_at,
            payload=artifact_payload,
        )
        try:
            self._repositories.artifacts.put(record)
        except ImmutableRecordConflict:
            existing_pair = self._repositories.artifacts.get_by_supply(
                scope,
                candidate_record.snapshot_id,
                radio_record.snapshot_id if radio_record else "",
                allocation_epoch=epoch,
            )
            if existing_pair is None:
                raise
            return self._attach_materialized(
                existing_pair,
                state=self._repositories.state.reconcile(scope),
            )
        try:
            if not state.active_artifact_id:
                self._repositories.state.initialize_active(
                    scope,
                    record.artifact_id,
                    expected_revision=state.revision,
                )
                return ComposeOutcome(
                    stored=True,
                    initialized_active=True,
                    artifact_id=record.artifact_id,
                    candidate_snapshot_id=candidate_record.snapshot_id,
                    radio_snapshot_id=radio_record.snapshot_id if radio_record else "",
                    reason="active_initialized",
                )
            self._repositories.state.append_ready(
                scope,
                record.artifact_id,
                expected_revision=state.revision,
            )
        except (QueueFull, StaleRevision, HomeRepositoryConflict):
            return ComposeOutcome(
                artifact_id=record.artifact_id,
                candidate_snapshot_id=candidate_record.snapshot_id,
                radio_snapshot_id=radio_record.snapshot_id if radio_record else "",
                reason="state_commit_conflict",
            )
        return ComposeOutcome(
            stored=True,
            artifact_id=record.artifact_id,
            candidate_snapshot_id=candidate_record.snapshot_id,
            radio_snapshot_id=radio_record.snapshot_id if radio_record else "",
            reason="ready_appended",
        )
