from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping
import time
import uuid

from ..discovery.enrichment import (
    build_enrichment_plan,
    complete_inventory_release_metadata,
    materialize_enrichment_plan,
)
from ..discovery.inventory import (
    CandidateInventory,
    build_candidate_inventory,
    candidate_inventory_from_payload,
    candidate_inventory_content_hash,
    candidate_inventory_content_identities,
    candidate_inventory_refresh_seed,
    candidate_inventory_supply_status,
    candidate_inventory_to_payload,
    candidate_inventory_track_identities,
    load_candidate_inventory,
    merge_candidate_inventories,
    refresh_candidate_inventory_coverage,
    store_candidate_inventory,
)
from ..discovery.source_registry import verify_materialized_supply
from .contracts import (
    ALBUM_UNEXPOSED_ARTIST_RESERVE_TARGET,
    CANDIDATE_SNAPSHOT_TTL_SECONDS,
    HOME_DISCOVERY_EXPOSURE_COOLDOWN_SECONDS,
    CandidateSnapshotRecord,
    artist_identity_aliases,
    home_supply_profile_fingerprint,
    normalize_artist_token,
    with_home_supply_profile,
)
from .repositories import HomeRepositorySet


GENERAL_CANDIDATE_POOLS = {
    "similarity",
    "profile_spine",
    "artist_graph",
    "genre_mood",
    "collaborative",
    "popularity",
    "album",
}


def _artifact_artist_aliases(
    repositories: HomeRepositorySet,
    user_scope_id: str,
) -> set[str]:
    state = repositories.state.reconcile(user_scope_id)
    aliases: set[str] = set()
    artifact_ids = (
        ([state.active_artifact_id] if state.active_artifact_id else [])
        + list(state.ready_artifact_ids)
    )
    for artifact_id in artifact_ids:
        record = repositories.artifacts.get(artifact_id)
        if record is None:
            continue
        for row in (record.payload or {}).get("rows") or []:
            if not isinstance(row, Mapping):
                continue
            for item in row.get("items") or []:
                if isinstance(item, Mapping):
                    aliases.update(artist_identity_aliases(item))
    return aliases


def _known_artist_aliases(
    repositories: HomeRepositorySet,
    taste: Any,
) -> set[str]:
    aliases = _artifact_artist_aliases(repositories, taste.user_scope_id)
    for identity in repositories.exposures.recent_artist_identities(
        taste.user_scope_id,
        since=time.time() - HOME_DISCOVERY_EXPOSURE_COOLDOWN_SECONDS,
    ):
        aliases.update(artist_identity_aliases(identity))
    for artist in (
        *list(getattr(taste, "listened_artists", []) or []),
        *list(getattr(taste, "top_artists", []) or []),
        *list(getattr(taste, "artist_hints", []) or []),
    ):
        aliases.update(artist_identity_aliases(str(artist or "")))
    return aliases


def _album_artist_key(item: Mapping[str, Any]) -> str:
    name = normalize_artist_token(item.get("artist") or item.get("artist_name"))
    if name:
        return f"artist:{name}"
    aliases = artist_identity_aliases(item)
    return next(
        (
            identity
            for identity in sorted(aliases)
            if identity.startswith(("musicbrainz:artist:", "provider:artist:"))
        ),
        "",
    )


def _apply_exposure_aware_album_reserve(
    inventory: CandidateInventory,
    *,
    repositories: HomeRepositorySet,
    taste: Any,
) -> CandidateInventory:
    known = _known_artist_aliases(repositories, taste)
    qualified: dict[str, set[str]] = {}
    for candidate in inventory.pools.get("album", []) or []:
        if candidate.item_type != "album" or candidate.item.get("playable") is not True:
            continue
        item = dict(candidate.item or {})
        key = _album_artist_key(item)
        if key:
            qualified.setdefault(key, set()).update(artist_identity_aliases(item))
    unexposed = {
        key for key, aliases in qualified.items() if not (aliases & known)
    }
    deficit = max(
        ALBUM_UNEXPOSED_ARTIST_RESERVE_TARGET - len(unexposed),
        0,
    )
    discovery_seeds: list[str] = []
    seen: set[str] = set()
    for pool_name in (
        "artist_graph",
        "similarity",
        "genre_mood",
        "collaborative",
        "popularity",
        "discovery_universe",
    ):
        for candidate in inventory.pools.get(pool_name, []) or []:
            item = dict(candidate.item or {})
            artist = str(
                item.get("artist")
                or item.get("artist_name")
                or item.get("name")
                or item.get("neighbor_artist")
                or ""
            ).strip()
            token = normalize_artist_token(artist)
            key = f"artist:{token}" if token else ""
            if (
                not key
                or key in seen
                or key in qualified
                or artist_identity_aliases(artist) & known
            ):
                continue
            seen.add(key)
            discovery_seeds.append(artist)
            if len(discovery_seeds) >= 32:
                break
        if len(discovery_seeds) >= 32:
            break
    ledger = dict(inventory.acquisition_ledger or {})
    shortages = dict(ledger.get("album_shelf_shortages") or {})
    if deficit:
        shortages["featured_new_albums"] = max(
            int(shortages.get("featured_new_albums") or 0),
            deficit,
        )
    ledger.update(
        {
            "album_shelf_shortages": shortages,
            "qualified_album_reserve_shortage": max(
                int(ledger.get("qualified_album_reserve_shortage") or 0),
                deficit,
            ),
            "album_unique_artist_count": len(qualified),
            "album_unexposed_artist_count": len(unexposed),
            "album_unexposed_artist_target": ALBUM_UNEXPOSED_ARTIST_RESERVE_TARGET,
            "album_unexposed_artist_deficit": deficit,
            "album_priority_artist_seeds": discovery_seeds,
            "album_expansion_artist_seeds": list(
                dict.fromkeys(
                    [
                        *discovery_seeds,
                        *list(ledger.get("album_expansion_artist_seeds") or []),
                    ]
                )
            )[:48],
        }
    )
    return replace(inventory, acquisition_ledger=ledger)


def _exposure_aware_supply_status(inventory: CandidateInventory) -> dict[str, Any]:
    status = dict(candidate_inventory_supply_status(inventory))
    deficit = int(
        (inventory.acquisition_ledger or {}).get(
            "album_unexposed_artist_deficit"
        )
        or 0
    )
    status.update(
        {
            "album_unexposed_artist_count": int(
                (inventory.acquisition_ledger or {}).get(
                    "album_unexposed_artist_count"
                )
                or 0
            ),
            "album_unexposed_artist_target": ALBUM_UNEXPOSED_ARTIST_RESERVE_TARGET,
            "album_unexposed_artist_deficit": deficit,
            "expansion_needed": bool(status.get("expansion_needed") or deficit > 0),
        }
    )
    return status


def exposure_aware_candidate_supply_status(
    inventory: CandidateInventory,
    *,
    repositories: HomeRepositorySet,
    user_scope_id: str,
) -> dict[str, Any]:
    """Evaluate reserve health against what Home has already exposed.

    API scheduling cannot trust the acquisition-time item totals stored in an
    immutable snapshot: exposure advances every time a feed is promoted. This
    lightweight view recomputes only artist novelty from durable artifacts and
    exposure history; it performs no provider or profile work.
    """

    class _ExposureTaste:
        def __init__(self, scope: str) -> None:
            self.user_scope_id = scope
            self.listened_artists: tuple[str, ...] = ()
            self.top_artists: tuple[str, ...] = ()
            self.artist_hints: tuple[str, ...] = ()

    evaluated = _apply_exposure_aware_album_reserve(
        inventory,
        repositories=repositories,
        taste=_ExposureTaste(user_scope_id),
    )
    return _exposure_aware_supply_status(evaluated)


def _general_candidate_content_hash(inventory: CandidateInventory) -> str:
    """Hash only supply owned by the general candidate worker.

    Older snapshots may still contain radio pools. Revalidation deliberately
    detaches those pools because radio has its own repository; that ownership
    cleanup is not a rewrite of the immutable general-candidate generation.
    """

    general_pools = {
        name: list(values or [])
        for name, values in (inventory.pools or {}).items()
        if name not in {"radio_artist_catalog", "popular_radio_cards"}
        and not name.startswith("radio_")
    }
    return candidate_inventory_content_hash(
        replace(inventory, pools=general_pools)
    )


@dataclass(frozen=True)
class CandidateSupplyOutcome:
    snapshot_id: str = ""
    ready: bool = False
    made_progress: bool = False
    scheduled_requests: int = 0
    candidate_count: int = 0
    expansion_needed: bool = False
    supply_status: dict[str, Any] | None = None
    retry_after_seconds: int = 0
    retryable: bool = False
    reason: str = ""


class CandidateSupplyService:
    """Sole owner of general Home candidate acquisition and publication."""

    def __init__(self, server: Any, repositories: HomeRepositorySet | None = None) -> None:
        self._server = server
        self._repositories = repositories or HomeRepositorySet.from_server(server)

    def _consolidate_inventory(
        self,
        taste: Any,
    ) -> tuple[CandidateInventory | None, bool]:
        """Merge historical request-profile fragments into one account supply."""

        stable_profile = home_supply_profile_fingerprint(taste.user_scope_id)
        current = load_candidate_inventory(
            self._server,
            taste.user_scope_id,
            profile_fingerprint=stable_profile,
            require_fresh=False,
            allow_profile_mismatch=True,
        )
        fragments: dict[str, CandidateInventory] = {}
        if current is not None and current.generation_id:
            fragments[current.generation_id] = current
        for record in self._repositories.candidates.recent(
            taste.user_scope_id,
            limit=12,
        ):
            inventory = candidate_inventory_from_payload(dict(record.payload or {}))
            if inventory is not None and inventory.generation_id:
                fragments.setdefault(inventory.generation_id, inventory)
        if not fragments:
            return None, False
        ordered = sorted(
            fragments.values(),
            key=lambda value: (float(value.generated_at or 0.0), value.generation_id),
            reverse=True,
        )
        consolidated = replace(ordered[0], profile_fingerprint=stable_profile)
        for fragment in ordered[1:]:
            consolidated = merge_candidate_inventories(
                consolidated,
                replace(fragment, profile_fingerprint=stable_profile),
            )
        consolidated = refresh_candidate_inventory_coverage(
            consolidated,
            taste=with_home_supply_profile(taste),
        )
        current_ids = candidate_inventory_content_identities(current)
        consolidated_ids = candidate_inventory_content_identities(consolidated)
        migration_required = bool(
            current is None
            or current.profile_fingerprint != stable_profile
            or current_ids != consolidated_ids
        )
        if not migration_required:
            return current, False
        now = time.time()
        freshness_source = ordered[0]
        migrated = replace(
            consolidated,
            profile_fingerprint=stable_profile,
            generation_id=str(uuid.uuid4()),
            base_generation_id=str(current.generation_id if current is not None else ""),
            # Consolidation changes ownership, not freshness. Retain the
            # newest source generation's verification clock so an expired
            # profile fragment still enters the real revalidation path.
            generated_at=float(freshness_source.generated_at or now),
            expires_at=float(
                freshness_source.expires_at
                or (
                    float(freshness_source.generated_at or now)
                    + CANDIDATE_SNAPSHOT_TTL_SECONDS
                )
            ),
            acquisition_ledger={
                **dict(consolidated.acquisition_ledger or {}),
                "profile_fragments_consolidated": len(ordered),
                "profile_consolidation_added_identity_count": len(
                    consolidated_ids - current_ids
                ),
                "supply_profile_version": "home-supply-v1",
                "fragment_migration_at": now,
            },
        )
        if store_candidate_inventory(self._server, migrated):
            return migrated, True
        concurrent = load_candidate_inventory(
            self._server,
            taste.user_scope_id,
            profile_fingerprint=stable_profile,
            require_fresh=False,
            allow_profile_mismatch=False,
        )
        return concurrent or migrated, True

    def _consolidated_inventory(self, taste: Any) -> CandidateInventory | None:
        """Compatibility wrapper used by focused inventory tests."""

        inventory, _changed = self._consolidate_inventory(taste)
        return inventory

    def refresh(
        self,
        taste: Any,
        *,
        force: bool = False,
        max_cycles: int = 1,
    ) -> CandidateSupplyOutcome:
        taste = with_home_supply_profile(taste)
        current, consolidation_changed = self._consolidate_inventory(taste)
        if current is not None:
            current = _apply_exposure_aware_album_reserve(
                current,
                repositories=self._repositories,
                taste=taste,
            )
        now = time.time()
        if (
            current is not None
            and current.is_ready
            and float(current.expires_at or 0.0) > now
            and not force
        ):
            supply_status = _exposure_aware_supply_status(current)
            snapshot_id = self._publish_snapshot(current)
            return CandidateSupplyOutcome(
                snapshot_id=snapshot_id,
                ready=True,
                made_progress=consolidation_changed,
                candidate_count=len(candidate_inventory_track_identities(current)),
                expansion_needed=bool(supply_status["expansion_needed"]),
                supply_status=supply_status,
                reason=(
                    "consolidated_inventory_ready"
                    if consolidation_changed
                    else "fresh_inventory_reused"
                ),
            )

        # Profile cutover or fragment consolidation already produced a new
        # immutable, ready supply revision. Publish it immediately; provider
        # expansion is maintenance and must not delay its first composition.
        if current is not None and current.is_ready and consolidation_changed:
            supply_status = _exposure_aware_supply_status(current)
            return CandidateSupplyOutcome(
                snapshot_id=self._publish_snapshot(current),
                ready=True,
                made_progress=True,
                candidate_count=len(candidate_inventory_track_identities(current)),
                expansion_needed=bool(supply_status["expansion_needed"]),
                supply_status=supply_status,
                reason="consolidated_inventory_ready",
            )

        previous = current
        if (
            previous is not None
            and float(previous.expires_at or 0.0) <= now
        ):
            previous = candidate_inventory_refresh_seed(
                previous,
                reason="revalidation_due",
            )
        refresh_required = bool(
            previous is not None
            and (previous.acquisition_ledger or {}).get(
                "candidate_refresh_required"
            )
        )
        before_ids = candidate_inventory_content_identities(previous)
        total_scheduled = 0
        built: CandidateInventory | None = previous
        made_progress = False

        for _cycle in range(max(1, min(int(max_cycles or 1), 4))):
            plan = build_enrichment_plan(
                taste,
                acquisition_ledger=(built.acquisition_ledger if built is not None else {}),
                allowed_pools=GENERAL_CANDIDATE_POOLS,
            )
            supply = materialize_enrichment_plan(
                self._server,
                plan,
                max_workers=6,
                max_pending_jobs=6,
            )
            scheduled = int((supply.diagnostics or {}).get("scheduled_request_count") or 0)
            total_scheduled += scheduled
            if (
                scheduled <= 0
                and not supply.pools
                and not any((built.pools or {}).values() if built is not None else ())
            ):
                # There is no retained material to rerank and no completed
                # acquisition to merge. Avoid invoking candidate builders on
                # incomplete test/guest profiles; a later scheduled pass can
                # supply the first facts.
                break
            if scheduled <= 0 and not supply.pools:
                verified = supply
            else:
                verified = verify_materialized_supply(
                    self._server,
                    supply,
                    taste,
                    max_new_verifications=16,
                    max_workers=4,
                    hydrate_artist_artwork=False,
                )
            candidate = build_candidate_inventory(
                self._server,
                taste,
                previous=built,
                materialized_supply=verified,
            )
            candidate = complete_inventory_release_metadata(
                self._server,
                candidate,
                # Release metadata has its own persisted enrichment lifecycle;
                # a candidate retry may consume cache hits but cannot perform
                # live metadata work on the feed-preparation clock.
                allow_remote_lookup=False,
            )
            candidate = _apply_exposure_aware_album_reserve(
                candidate,
                repositories=self._repositories,
                taste=taste,
            )
            after_ids = candidate_inventory_content_identities(candidate)
            added_ids = after_ids - before_ids
            content_changed = after_ids != before_ids
            prior_no_progress = int(
                (built.acquisition_ledger if built is not None else {}).get(
                    "consecutive_no_progress_batches"
                )
                or 0
            )
            no_progress_count = 0 if added_ids else prior_no_progress + 1
            ledger = {
                **dict(candidate.acquisition_ledger or {}),
                "candidate_content_hash": candidate_inventory_content_hash(candidate),
                "last_added_identity_count": len(added_ids),
                "consecutive_no_progress_batches": no_progress_count,
                "candidate_refresh_required": (
                    False
                    if added_ids
                    else bool(
                        (candidate.acquisition_ledger or {}).get(
                            "candidate_refresh_required"
                        )
                    )
                ),
                "last_supply_progress_at": (
                    time.time()
                    if added_ids
                    else float(
                        (built.acquisition_ledger if built is not None else {}).get(
                            "last_supply_progress_at"
                        )
                        or 0.0
                    )
                ),
            }
            candidate = replace(candidate, acquisition_ledger=ledger)
            if built is not None and not content_changed:
                # Persist cursor/backoff progress in the mutable working
                # inventory without manufacturing a new immutable supply
                # revision from identical usable identities.
                candidate = replace(
                    candidate,
                    generation_id=built.generation_id,
                    base_generation_id=built.base_generation_id,
                    generated_at=built.generated_at,
                    expires_at=built.expires_at,
                )
            expected = str(built.generation_id or "") if built is not None else None
            if not store_candidate_inventory(
                self._server,
                candidate,
                expected_ready_generation_id=expected,
            ):
                concurrent = load_candidate_inventory(
                    self._server,
                    taste.user_scope_id,
                    profile_fingerprint=taste.profile_key,
                    require_fresh=False,
                    allow_profile_mismatch=False,
                )
                if concurrent is None:
                    return CandidateSupplyOutcome(
                        scheduled_requests=total_scheduled,
                        retryable=True,
                        reason="candidate_publication_conflict",
                    )
                built = concurrent
            else:
                built = candidate
            after_ids = candidate_inventory_content_identities(built)
            made_progress = made_progress or bool(added_ids)
            before_ids = after_ids
            if built.is_ready:
                break
            # A zero-request pass still publishes a new ranked generation
            # from retained verified facts. Do not spin on the same empty plan.
            if scheduled <= 0 and not supply.pools:
                break

        if built is None:
            return CandidateSupplyOutcome(
                scheduled_requests=total_scheduled,
                retry_after_seconds=300,
                retryable=False,
                reason="candidate_supply_unavailable",
            )
        refresh_incomplete = refresh_required and not made_progress
        # Expiry means acquisition must continue; it does not invalidate
        # already verified facts. Keep the usable snapshot available while
        # the bounded background lane replenishes its unseen reserve.
        snapshot_id = self._publish_snapshot(built) if built.is_ready else ""
        count = len(candidate_inventory_track_identities(built))
        supply_status = _exposure_aware_supply_status(built)
        no_progress_count = int(
            (built.acquisition_ledger or {}).get("consecutive_no_progress_batches")
            or 0
        )
        retry_after = min(300 * (2 ** max(no_progress_count - 1, 0)), 3600)
        return CandidateSupplyOutcome(
            snapshot_id=snapshot_id,
            ready=bool(built.is_ready),
            made_progress=made_progress,
            scheduled_requests=total_scheduled,
            candidate_count=count,
            expansion_needed=bool(supply_status["expansion_needed"]),
            supply_status=supply_status,
            retry_after_seconds=(retry_after if not made_progress else 0),
            retryable=not built.is_ready and made_progress,
            reason=(
                "candidate_snapshot_reused_no_progress"
                if refresh_incomplete
                else
                "candidate_snapshot_ready"
                if built.is_ready and made_progress
                else "candidate_snapshot_reused"
                if built.is_ready
                else "candidate_no_progress"
                if not made_progress
                else "candidate_contract_incomplete"
                if total_scheduled > 0
                else "no_scheduled_candidate_work"
            ),
        )

    def _publish_snapshot(self, inventory: CandidateInventory) -> str:
        snapshot_id = str(inventory.generation_id or "").strip()
        if not snapshot_id:
            raise ValueError("candidate inventory generation_id is required")
        existing = self._repositories.candidates.get(snapshot_id)
        if existing is not None:
            existing_inventory = candidate_inventory_from_payload(
                dict(existing.payload or {})
            )
            if (
                existing.user_scope_id == inventory.user_scope_id
                and existing.profile_fingerprint == inventory.profile_fingerprint
                and existing_inventory is not None
                and _general_candidate_content_hash(existing_inventory)
                == _general_candidate_content_hash(inventory)
            ):
                # The mutable working inventory may advance acquisition
                # cursors/backoff without adding usable identities. That is
                # not a new immutable supply revision and must not rewrite the
                # snapshot already consumed by composition.
                return snapshot_id
        generated_at = float(inventory.generated_at or time.time())
        expires_at = min(
            float(inventory.expires_at or generated_at + CANDIDATE_SNAPSHOT_TTL_SECONDS),
            generated_at + CANDIDATE_SNAPSHOT_TTL_SECONDS,
        )
        self._repositories.candidates.put(
            CandidateSnapshotRecord(
                snapshot_id=snapshot_id,
                user_scope_id=inventory.user_scope_id,
                profile_fingerprint=inventory.profile_fingerprint,
                generated_at=generated_at,
                expires_at=expires_at,
                payload=candidate_inventory_to_payload(inventory),
            )
        )
        return snapshot_id
