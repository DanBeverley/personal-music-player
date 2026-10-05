from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping
import hashlib
import json
import time
import uuid

from ..discovery.enrichment import build_enrichment_plan, materialize_enrichment_plan
from ..discovery.inventory import (
    build_candidate_inventory,
    canonical_item_identity,
    candidate_inventory_from_payload,
    load_candidate_inventory,
)
from ..discovery.radio_inventory import (
    RADIO_DISCOVERY_RESERVE_TARGET_CARDS,
    RADIO_WORKING_RESERVOIR_TARGET_CARDS,
    RADIO_VISIBLE_DISCOVERY_TARGET,
    ArtistRadioInventory,
    artist_radio_inventory_from_payload,
    artist_radio_inventory_to_payload,
    build_artist_radio_inventory,
    load_artist_radio_inventory,
    merge_store_artist_radio_inventory,
    select_radio_rotation,
)
from ..discovery.source_registry import verify_materialized_supply
from ..discovery.ranking import admitted_artist_candidates
from ..discovery.structured_providers import LastFmClient
from ..search.intelligence import remember_catalog_entity
from ..storage.artist_artwork import cache_artist_artwork_now
from .contracts import (
    HOME_DISCOVERY_EXPOSURE_COOLDOWN_SECONDS,
    RADIO_SNAPSHOT_TTL_SECONDS,
    RadioSnapshotRecord,
    artist_identity_aliases,
)
from .repositories import HomeRepositorySet


DISCOVERY_SOURCE_POOLS = (
    "artist_graph",
    "similarity",
    "popularity",
    "genre_mood",
    "collaborative",
    "discovery_universe",
)

def _artist_name(item: Mapping[str, Any]) -> str:
    return str(
        item.get("artist")
        or item.get("artist_name")
        or item.get("author")
        or item.get("channel_name")
        or (
            item.get("name")
            if item.get("canonical_artist_id")
            or item.get("musicbrainz_artist_id")
            or item.get("provider_artist_id")
            else ""
        )
        or ""
    ).strip()


def _artist_identity(item: Mapping[str, Any]) -> str:
    canonical = str(
        item.get("canonical_artist_id")
        or item.get("artwork_cache_identity")
        or ""
    ).strip()
    if canonical:
        normalized = canonical.casefold()
        if normalized.startswith(("musicbrainz:artist:", "provider:artist:")):
            return normalized
        return f"musicbrainz:artist:{normalized}"
    mbid = str(
        item.get("musicbrainz_artist_id")
        or item.get("artist_mbid")
        or item.get("mb_artist_id")
        or ""
    ).strip()
    if mbid:
        normalized = mbid.casefold()
        return (
            normalized
            if normalized.startswith("musicbrainz:artist:")
            else f"musicbrainz:artist:{normalized}"
        )
    provider = str(
        item.get("provider_artist_id")
        or item.get("artist_id")
        or item.get("browseId")
        or ""
    ).strip()
    if provider:
        return f"provider:artist:{provider.casefold()}"
    return ""


def _radio_semantic_payload(
    inventory: ArtistRadioInventory | None,
) -> list[dict[str, Any]]:
    """Return verified user-visible supply, excluding retry metadata."""
    if inventory is None:
        return []
    output: list[dict[str, Any]] = []
    for card in inventory.reservoir_cards or inventory.cards or []:
        if not isinstance(card, Mapping):
            continue
        identity = str(
            card.get("seed_artist_identity")
            or card.get("seed_artist_key")
            or card.get("id")
            or ""
        ).strip().casefold()
        if not identity:
            continue
        track_ids: set[str] = set()
        for track in card.get("tracks") or card.get("items") or []:
            if not isinstance(track, Mapping):
                continue
            track_id = canonical_item_identity(dict(track), item_type="track")
            if track_id:
                track_ids.add(track_id)
        output.append(
            {
                "identity": identity,
                "mode": str(card.get("radio_mode") or "").strip().casefold(),
                "affinity": str(card.get("seed_affinity") or "").strip().casefold(),
                "artwork_owner": str(
                    card.get("artwork_owner_identity")
                    or card.get("seed_artist_identity")
                    or ""
                ).strip().casefold(),
                "thumbnail": str(card.get("thumbnail") or "").strip(),
                "tracks": sorted(track_ids),
            }
        )
    output.sort(key=lambda value: (value["identity"], value["mode"]))
    return output


def _radio_semantic_hash(inventory: ArtistRadioInventory | None) -> str:
    encoded = json.dumps(
        _radio_semantic_payload(inventory),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RadioSupplyOutcome:
    snapshot_id: str = ""
    ready: bool = False
    made_progress: bool = False
    scheduled_requests: int = 0
    reservoir_count: int = 0
    discovery_count: int = 0
    seed_candidates_offered: int = 0
    source_verification_verified: int = 0
    new_card_count: int = 0
    artwork_records: tuple[Mapping[str, Any], ...] = ()
    expansion_needed: bool = False
    retryable: bool = False
    reason: str = ""


def _radio_expansion_needed(inventory: ArtistRadioInventory | None) -> bool:
    if inventory is None:
        return True
    diagnostics = dict(inventory.diagnostics or {})
    reservoir = list(inventory.reservoir_cards or inventory.cards or [])
    discovery_count = int(
        diagnostics.get("reservoir_discovery_card_count")
        or diagnostics.get("discovery_card_count")
        or 0
    )
    return bool(
        len(reservoir) < RADIO_WORKING_RESERVOIR_TARGET_CARDS
        or discovery_count < RADIO_DISCOVERY_RESERVE_TARGET_CARDS
        or int(diagnostics.get("unexposed_deficit") or 0) > 0
        or int(diagnostics.get("visible_discovery_target_deficit") or 0) > 0
    )


def _profile_known_artist_identities(taste: Any) -> set[str]:
    output: set[str] = set()
    for value in (
        *list(getattr(taste, "listened_artists", []) or []),
        *list(getattr(taste, "top_artists", []) or []),
        *list(getattr(taste, "artist_hints", []) or []),
    ):
        output.update(artist_identity_aliases(str(value or "")))
    return output


def _radio_known_artist_identities(
    repositories: HomeRepositorySet,
    taste: Any,
) -> set[str]:
    """Return real familiarity evidence for Popular Radio.

    Other recommendation rows are presentation, not user behavior. Counting
    every artist in active and queued artifacts made a healthy radio reservoir
    report zero discovery and caused the composer to remove the row.
    """

    output = _profile_known_artist_identities(taste)
    for identity in repositories.exposures.recent_radio_artist_identities(
        taste.user_scope_id,
        since=time.time() - HOME_DISCOVERY_EXPOSURE_COOLDOWN_SECONDS,
    ):
        output.update(artist_identity_aliases(identity))
    return output


def _qualified_discovery_count(diagnostics: Mapping[str, Any] | None) -> int:
    """Read the profile-aware count before the legacy affinity-only count."""
    values = dict(diagnostics or {})
    return int(
        values.get("reservoir_discovery_card_count")
        or values.get("selected_discovery_card_count")
        or values.get("discovery_card_count")
        or 0
    )


class RadioSupplyService:
    """Sole owner of radio acquisition, qualification, and publication."""

    def __init__(self, server: Any, repositories: HomeRepositorySet | None = None) -> None:
        self._server = server
        self._repositories = repositories or HomeRepositorySet.from_server(server)

    def refresh(
        self,
        taste: Any,
        *,
        candidate_snapshot_id: str = "",
    ) -> RadioSupplyOutcome:
        candidates = None
        known_artist_identities = _radio_known_artist_identities(
            self._repositories,
            taste,
        )
        if candidate_snapshot_id:
            candidate_record = self._repositories.candidates.get(
                candidate_snapshot_id
            )
            if (
                candidate_record is not None
                and candidate_record.user_scope_id == taste.user_scope_id
                and candidate_record.is_usable()
            ):
                candidates = candidate_inventory_from_payload(
                    dict(candidate_record.payload)
                )
        else:
            candidates = load_candidate_inventory(
                self._server,
                taste.user_scope_id,
                profile_fingerprint=taste.profile_key,
                require_fresh=False,
                allow_profile_mismatch=False,
            )
        existing = load_artist_radio_inventory(
            self._server,
            taste.user_scope_id,
            profile_fingerprint=taste.profile_key,
            allow_profile_mismatch=True,
            allow_expired=True,
        )
        if existing is not None:
            # Cover the fresh-reservoir reuse path as well as acquisition.
            if not merge_store_artist_radio_inventory(self._server, existing):
                return RadioSupplyOutcome(reason="radio_source_revalidation_failed", retryable=True)
            existing = load_artist_radio_inventory(
                self._server,
                taste.user_scope_id,
                allow_profile_mismatch=True,
                allow_expired=True,
            )
        if existing is not None and existing.profile_fingerprint != taste.profile_key:
            # Radio supply is account-owned like general candidates. Preserve
            # the qualified reservoir while moving legacy request-profile
            # storage onto the stable Home supply identity.
            now = time.time()
            existing = replace(
                existing,
                profile_fingerprint=taste.profile_key,
                generated_at=now,
                expires_at=max(float(existing.expires_at or 0.0), now + RADIO_SNAPSHOT_TTL_SECONDS),
            )
            merge_store_artist_radio_inventory(self._server, existing)
        # A qualified persisted reservoir is already all the supply needed to
        # publish a feed rotation. Materialize its visible slice locally before
        # considering provider acquisition. This also repairs the historical
        # state where ``reservoir_cards`` was populated but ``cards`` stayed
        # empty forever.
        selected_existing = select_radio_rotation(
            existing,
            known_artist_identities=known_artist_identities,
            epoch=max(
                int(
                    (getattr(existing, "diagnostics", {}) or {}).get(
                        "rotation_epoch"
                    )
                    or 0
                ),
                0,
            ),
        )
        existing_is_fresh = bool(
            existing is not None
            and float(getattr(existing, "generated_at", 0.0) or 0.0) <= time.time()
            and float(getattr(existing, "expires_at", 0.0) or 0.0) > time.time()
        )
        base_tracks = sum(
            len(values or [])
            for name, values in (getattr(candidates, "pools", {}) or {}).items()
            if name not in {"album", "radio_artist_catalog", "popular_radio_cards"}
        ) if candidates is not None else 0
        servable_existing = (
            selected_existing
            if existing_is_fresh
            and selected_existing is not None
            and selected_existing.is_ready
            else None
        )
        # Readiness means this rotation may be served; it does not mean the
        # reservoir has enough discovery supply. Continue acquisition whenever
        # useful candidate seeds exist and the reservoir is still deficient.
        if servable_existing is not None and (
            not _radio_expansion_needed(servable_existing) or base_tracks <= 0
        ):
            if not list(getattr(existing, "cards", []) or []):
                merge_store_artist_radio_inventory(self._server, servable_existing)
            final_cards = list(servable_existing.cards or [])
            final_diag = dict(servable_existing.diagnostics or {})
            return RadioSupplyOutcome(
                snapshot_id=self._publish_snapshot(servable_existing),
                ready=True,
                made_progress=not bool(getattr(existing, "cards", []) or []),
                scheduled_requests=0,
                reservoir_count=len(
                    servable_existing.reservoir_cards or final_cards
                ),
                discovery_count=int(
                    final_diag.get("selected_discovery_card_count")
                    or final_diag.get("discovery_card_count")
                    or 0
                ),
                expansion_needed=_radio_expansion_needed(servable_existing),
                reason="ready_reservoir_reused",
            )
        # Radio can expand from base track/artist supply even when optional
        # album shelves are incomplete. Only require a fresh candidate record
        # with usable track/artist material when no ready reservoir exists.
        if candidates is None or (
            base_tracks <= 0 and not bool(getattr(candidates, "is_ready", False))
        ):
            if servable_existing is not None:
                return RadioSupplyOutcome(
                    snapshot_id=self._publish_snapshot(servable_existing),
                    ready=True,
                    reservoir_count=len(
                        servable_existing.reservoir_cards
                        or servable_existing.cards
                        or []
                    ),
                    discovery_count=int(
                        (servable_existing.diagnostics or {}).get(
                            "selected_discovery_card_count"
                        )
                        or 0
                    ),
                    expansion_needed=_radio_expansion_needed(servable_existing),
                    reason="ready_reservoir_reused",
                )
            return RadioSupplyOutcome(
                retryable=True,
                reason="fresh_candidate_snapshot_required",
            )
        before_cards = list(
            getattr(existing, "reservoir_cards", [])
            or getattr(existing, "cards", [])
            or []
        )
        before_semantic_hash = _radio_semantic_hash(existing)
        before_identity_ids = {
            str(
                card.get("seed_artist_identity")
                or card.get("seed_artist_key")
                or card.get("id")
                or ""
            ).strip().casefold()
            for card in before_cards
            if isinstance(card, Mapping)
        }
        existing_diag = dict(
            getattr(selected_existing or existing, "diagnostics", {}) or {}
        )
        own_counts = {
            str(key).casefold(): int(value or 0)
            for key, value in dict(existing_diag.get("own_track_count_by_seed") or {}).items()
        }
        discovery_seeds = self._discovery_seeds(
            candidates,
            taste,
            before_cards,
            own_counts=own_counts,
            exposed_artist_identities=(
                *list(existing_diag.get("popular_radio_exposure_ledger") or []),
                *list(known_artist_identities),
            ),
        )
        discovery_count = _qualified_discovery_count(existing_diag)
        reservoir_count = len(before_cards)
        exposure_deficit = int(existing_diag.get("exposure_deficit") or 0)
        progress = dict(existing_diag.get("radio_expansion_progress") or {})
        ledger = {
            **dict(candidates.acquisition_ledger or {}),
            **{
                target: progress[source]
                for source, target in (
                    ("anchor_cursor_next", "anchor_cursor"),
                    ("artist_cursor_next", "artist_cursor"),
                    ("radio_discovery_cursor_next", "radio_discovery_cursor"),
                    ("request_progress", "request_progress"),
                )
                if progress.get(source) is not None
            },
            "radio_seed_counts": own_counts,
        }
        plan = build_enrichment_plan(
            taste,
            acquisition_ledger=ledger,
            allowed_pools={"radio_artist_catalog"},
            radio_discovery_artist_seeds=discovery_seeds,
            radio_discovery_deficit=max(
                RADIO_DISCOVERY_RESERVE_TARGET_CARDS - discovery_count,
                exposure_deficit,
                0,
            ),
            radio_reservoir_deficit=max(
                RADIO_WORKING_RESERVOIR_TARGET_CARDS - reservoir_count,
                exposure_deficit,
                0,
            ),
        )
        supply = materialize_enrichment_plan(
            self._server,
            plan,
            max_workers=4,
            max_pending_jobs=4,
        )
        scheduled = int((supply.diagnostics or {}).get("scheduled_request_count") or 0)
        if scheduled <= 0 and not supply.pools:
            if servable_existing is not None:
                return RadioSupplyOutcome(
                    snapshot_id=self._publish_snapshot(servable_existing),
                    ready=True,
                    reservoir_count=len(
                        servable_existing.reservoir_cards
                        or servable_existing.cards
                        or []
                    ),
                    discovery_count=int(
                        (servable_existing.diagnostics or {}).get(
                            "selected_discovery_card_count"
                        )
                        or 0
                    ),
                    expansion_needed=_radio_expansion_needed(servable_existing),
                    reason="ready_reservoir_reused_no_expansion_work",
                )
            return RadioSupplyOutcome(
                ready=bool(existing and existing.is_ready),
                reservoir_count=reservoir_count,
                discovery_count=discovery_count,
                expansion_needed=bool(
                    existing and _radio_expansion_needed(existing)
                ),
                retryable=not bool(existing and existing.is_ready),
                reason="no_scheduled_radio_work",
            )
        verified = verify_materialized_supply(
            self._server,
            supply,
            taste,
            # Finish the nearest two artist-card deficits in one pass instead
            # of spreading sixteen verifications across several incomplete
            # cards and scheduling another full acquisition cycle.
            max_new_verifications=24,
            max_workers=4,
            hydrate_artist_artwork=False,
        )
        enriched = build_candidate_inventory(
            self._server,
            taste,
            previous=candidates,
            materialized_supply=verified,
        )
        built = build_artist_radio_inventory(
            taste,
            enriched.pools,
            server=self._server,
        )
        built.diagnostics["radio_supply_funnel"] = {
            "seed_candidates_offered": len(discovery_seeds),
            "requests_scheduled": scheduled,
            "requests_completed": int(
                (supply.diagnostics or {}).get("completed_request_count") or 0
            ),
            "requests_failed": int(
                (supply.diagnostics or {}).get("failed_request_count") or 0
            ),
            "source_verification_attempted": int(
                (verified.diagnostics or {}).get("source_verification_attempted") or 0
            ),
            "source_verification_verified": int(
                (verified.diagnostics or {}).get("source_verification_verified") or 0
            ),
            "cards_before": len(before_cards),
            "cards_built": len(built.reservoir_cards or built.cards or []),
            "cards_rejected": dict(
                (built.diagnostics or {}).get("rejected") or {}
            ),
            "artwork_pending": int(
                (built.diagnostics or {}).get("artwork_repair_pending_count") or 0
            ),
        }
        built.diagnostics["radio_expansion_progress"] = {
            key: (supply.diagnostics or {}).get(key)
            for key in (
                "anchor_cursor_next",
                "artist_cursor_next",
                "radio_discovery_cursor_next",
                "request_progress",
                "completed_request_count",
            )
            if (supply.diagnostics or {}).get(key) is not None
        }
        merge_store_artist_radio_inventory(self._server, built)
        final = load_artist_radio_inventory(
            self._server,
            taste.user_scope_id,
            profile_fingerprint=taste.profile_key,
            allow_profile_mismatch=False,
            allow_expired=True,
        ) or built
        selected_final = select_radio_rotation(
            final,
            known_artist_identities=known_artist_identities,
            epoch=max(
                int((getattr(final, "diagnostics", {}) or {}).get("rotation_epoch") or 0),
                0,
            ),
        )
        if selected_final is not None:
            final = selected_final
        final_cards = list(final.reservoir_cards or final.cards or [])
        final_diag = dict(final.diagnostics or {})
        final_identity_ids = {
            str(
                card.get("seed_artist_identity")
                or card.get("seed_artist_key")
                or card.get("id")
                or ""
            ).strip().casefold()
            for card in final_cards
            if isinstance(card, Mapping)
        }
        after_semantic_hash = _radio_semantic_hash(final)
        new_identity_ids = final_identity_ids - before_identity_ids
        qualified_new_identity_ids = {
            str(
                card.get("seed_artist_identity")
                or card.get("seed_artist_key")
                or card.get("id")
                or ""
            ).strip().casefold()
            for card in final_cards
            if isinstance(card, Mapping)
            and str(
                card.get("seed_artist_identity")
                or card.get("seed_artist_key")
                or card.get("id")
                or ""
            ).strip().casefold() in new_identity_ids
            and not (artist_identity_aliases(card) & known_artist_identities)
        }
        made_progress = bool(qualified_new_identity_ids)
        funnel = dict(final_diag.get("radio_supply_funnel") or {})
        funnel.update(
            {
                "cards_after": len(final_cards),
                "new_card_count": len(qualified_new_identity_ids),
                "new_card_identity_count": len(new_identity_ids),
                "discovery_cards_after": _qualified_discovery_count(final_diag),
                "semantic_supply_changed": bool(
                    after_semantic_hash
                    and after_semantic_hash != before_semantic_hash
                ),
                "globally_unseen_supply_changed": made_progress,
            }
        )
        final = replace(
            final,
            diagnostics={**final_diag, "radio_supply_funnel": funnel},
        )
        final_diag = dict(final.diagnostics or {})
        snapshot_id = self._publish_snapshot(final) if final.is_ready else ""
        # Reserve deficits remain actionable even when the current provider
        # batch made no progress. Tying this flag to progress previously made
        # a shallow 22/4 reservoir look complete forever.
        expansion_needed = bool(final.is_ready and _radio_expansion_needed(final))
        artwork_records = sorted(
            (dict(value) for value in built.artwork_repair_records),
            key=lambda value: (
                0
                if str(value.get("seed_affinity") or "").strip().casefold()
                in {"related", "exploratory", "similar"}
                else 1,
                str(
                    value.get("canonical_artist_id")
                    or value.get("provider_artist_id")
                    or value.get("name")
                    or ""
                ).casefold(),
            ),
        )
        return RadioSupplyOutcome(
            snapshot_id=snapshot_id,
            ready=final.is_ready,
            made_progress=made_progress,
            scheduled_requests=scheduled,
            reservoir_count=len(final_cards),
            discovery_count=_qualified_discovery_count(final_diag),
            seed_candidates_offered=int(
                funnel.get("seed_candidates_offered") or 0
            ),
            source_verification_verified=int(
                funnel.get("source_verification_verified") or 0
            ),
            new_card_count=int(funnel.get("new_card_count") or 0),
            artwork_records=tuple(artwork_records),
            expansion_needed=expansion_needed,
            retryable=not final.is_ready,
            reason="radio_snapshot_ready" if final.is_ready else "radio_contract_incomplete",
        )

    def recompose(self, taste: Any) -> RadioSupplyOutcome:
        candidates = load_candidate_inventory(
            self._server,
            taste.user_scope_id,
            profile_fingerprint=taste.profile_key,
            require_fresh=False,
            allow_profile_mismatch=False,
        )
        if candidates is None:
            return RadioSupplyOutcome(retryable=True, reason="candidate_snapshot_missing")
        built = build_artist_radio_inventory(taste, candidates.pools, server=self._server)
        merge_store_artist_radio_inventory(self._server, built)
        final = load_artist_radio_inventory(
            self._server,
            taste.user_scope_id,
            profile_fingerprint=taste.profile_key,
            allow_profile_mismatch=False,
            allow_expired=True,
        ) or built
        selected_final = select_radio_rotation(
            final,
            known_artist_identities=_radio_known_artist_identities(
                self._repositories,
                taste,
            ),
        )
        if selected_final is not None:
            final = selected_final
        snapshot_id = self._publish_snapshot(final) if final.is_ready else ""
        return RadioSupplyOutcome(
            snapshot_id=snapshot_id,
            ready=final.is_ready,
            reservoir_count=len(final.reservoir_cards or final.cards or []),
            discovery_count=_qualified_discovery_count(final.diagnostics),
            artwork_records=tuple(dict(value) for value in built.artwork_repair_records),
            retryable=not final.is_ready,
            reason="radio_recomposed" if final.is_ready else "radio_artwork_still_incomplete",
        )

    def _discovery_seeds(
        self,
        inventory: Any,
        taste: Any,
        existing_cards: list[Mapping[str, Any]],
        *,
        own_counts: Mapping[str, int],
        exposed_artist_identities: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        familiar = {
            str(value).strip().casefold()
            for value in (
                *list(getattr(taste, "listened_artists", []) or []),
                *list(getattr(taste, "top_artists", []) or []),
                *list(getattr(taste, "artist_hints", []) or []),
            )
            if str(value).strip()
        }
        occupied = {
            str(
                card.get("seed_artist_identity")
                or card.get("seed_artist_key")
                or card.get("artist_name")
                or ""
            ).strip().casefold()
            for card in existing_cards
            if isinstance(card, Mapping)
        }
        occupied.update(
            str(value or "").strip().casefold()
            for value in exposed_artist_identities
            if str(value or "").strip()
        )
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        lanes = [list(admitted_artist_candidates(
            inventory.pools, taste, source_pools=(name,), row_kind="popular_radio",
        )) for name in DISCOVERY_SOURCE_POOLS]
        for index in range(max((len(lane) for lane in lanes), default=0)):
            for pool_name, lane in zip(DISCOVERY_SOURCE_POOLS, lanes):
                if index >= len(lane):
                    continue
                item = getattr(lane[index], "item", lane[index])
                if not isinstance(item, Mapping):
                    continue
                name = _artist_name(item)
                identity = _artist_identity(item)
                key = identity or name.casefold()
                if (
                    not name
                    or not identity
                    or name.casefold() in familiar
                    or key in occupied
                    or key in seen
                ):
                    continue
                seen.add(key)
                output.append(
                    {
                        "key": key,
                        "name": name,
                        "musicbrainz_artist_id": (
                            identity.removeprefix("musicbrainz:artist:")
                            if identity.startswith("musicbrainz:artist:")
                            else item.get("musicbrainz_artist_id")
                        ),
                        "provider_artist_id": (
                            identity.removeprefix("provider:artist:")
                            if identity.startswith("provider:artist:")
                            else item.get("provider_artist_id") or item.get("artist_id")
                        ),
                        "source": pool_name,
                    }
                )
                if len(output) >= 12:
                    return output
        return output

    def _publish_snapshot(self, inventory: ArtistRadioInventory) -> str:
        if not str(inventory.generation_id or "").strip():
            raise ValueError("radio inventory generation_id is required")
        latest = self._repositories.radio.latest_fresh(
            inventory.user_scope_id,
            profile_fingerprint=inventory.profile_fingerprint,
        )
        if latest is None:
            # Profile migration changes storage ownership, not customer-visible
            # radio supply. Reuse the legacy immutable ID when its verified
            # semantic content is identical.
            latest = self._repositories.radio.latest_usable(
                inventory.user_scope_id
            )
        if latest is not None:
            latest_inventory = artist_radio_inventory_from_payload(
                dict(latest.payload or {})
            )
            if _radio_semantic_hash(latest_inventory) == _radio_semantic_hash(
                inventory
            ):
                return latest.snapshot_id
        generated_at = time.time()
        expires_at = generated_at + RADIO_SNAPSHOT_TTL_SECONDS
        payload = artist_radio_inventory_to_payload(inventory)
        content_hash = _radio_semantic_hash(inventory)
        freshness_epoch = int(generated_at // RADIO_SNAPSHOT_TTL_SECONDS)
        snapshot_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "auralis:home-radio:"
                f"{inventory.user_scope_id}:{inventory.profile_fingerprint}:"
                f"{freshness_epoch}:{content_hash}",
            )
        )
        # Retry metadata, cursor movement and timestamps do not constitute new
        # customer-visible supply. Reuse the immutable semantic snapshot.
        if self._repositories.radio.get(snapshot_id) is not None:
            return snapshot_id
        self._repositories.radio.put(
            RadioSnapshotRecord(
                snapshot_id=snapshot_id,
                user_scope_id=inventory.user_scope_id,
                profile_fingerprint=inventory.profile_fingerprint,
                generated_at=generated_at,
                expires_at=expires_at,
                payload=payload,
            )
        )
        return snapshot_id


class ArtworkSupplyService:
    """Sole blocking owner of Home artist-artwork repair jobs."""

    def __init__(self, server: Any) -> None:
        self._server = server

    @staticmethod
    def _thumbnail_urls(value: Mapping[str, Any]) -> list[str]:
        output: list[str] = []
        for raw in value.get("thumbnails") or []:
            if isinstance(raw, Mapping):
                url = str(raw.get("url") or "").strip()
                if url.startswith(("http://", "https://")):
                    output.append(url)
        for key in ("artist_thumbnail", "artwork_source_url"):
            url = str(value.get(key) or "").strip()
            if url.startswith(("http://", "https://")):
                output.append(url)
        return list(dict.fromkeys(output))

    def _resolve_sources(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve portraits in the artwork worker, outside feed composition."""

        resolved = dict(record)
        artist = str(record.get("name") or record.get("artist") or "").strip()
        if not artist:
            return resolved
        urls = self._thumbnail_urls(resolved)
        client = getattr(self._server, "ytmusic", None)
        if client is not None:
            wanted = " ".join(artist.casefold().split())
            expected_provider_id = str(
                record.get("provider_artist_id")
                or record.get("artist_id")
                or ""
            ).strip()
            matches: list[Any] = []
            if not expected_provider_id:
                try:
                    matches = client.search(
                        artist,
                        filter="artists",
                        limit=8,
                    ) or []
                except Exception:
                    matches = []
            match = next(
                (
                    value
                    for value in matches
                    if isinstance(value, Mapping)
                    and " ".join(
                        str(value.get("artist") or value.get("name") or "")
                        .strip().casefold().split()
                    ) == wanted
                ),
                None,
            )
            if isinstance(match, Mapping):
                artist_id = str(match.get("browseId") or match.get("id") or "").strip()
                urls.extend(self._thumbnail_urls(match))
                if artist_id:
                    resolved.setdefault("provider_artist_id", artist_id)
                    resolved.setdefault(
                        "canonical_artist_id",
                        f"provider:artist:{artist_id.casefold()}",
                    )
            artist_id = expected_provider_id or str(
                resolved.get("provider_artist_id") or ""
            ).strip()
            if artist_id and hasattr(client, "get_artist"):
                try:
                    detail = client.get_artist(artist_id) or {}
                except Exception:
                    detail = {}
                urls.extend(self._thumbnail_urls(detail))
        try:
            lastfm = LastFmClient(self._server).artist_info(
                artist,
                artist_mbid=str(record.get("musicbrainz_artist_id") or "").strip(),
            )
        except Exception:
            lastfm = {}
        urls.extend(self._thumbnail_urls(lastfm))
        urls.extend(
            str(value or "").strip()
            for value in lastfm.get("artwork_source_urls") or []
            if str(value or "").strip().startswith(("http://", "https://"))
        )
        urls = list(dict.fromkeys(urls))
        if urls:
            resolved["artwork_source_url"] = urls[0]
            resolved["artwork_source_urls"] = urls
            resolved.setdefault("artist_thumbnail", urls[0])
        for key in ("musicbrainz_artist_id", "canonical_artist_id"):
            if lastfm.get(key) and not resolved.get(key):
                resolved[key] = lastfm[key]
        return resolved

    def repair(self, record: Mapping[str, Any]) -> bool:
        updated = cache_artist_artwork_now(
            self._server,
            self._resolve_sources(record),
        )
        if updated is None or not str(updated.get("thumbnail") or "").startswith(
            "/artist_artwork/"
        ):
            return False
        name = str(updated.get("name") or "").strip()
        if name:
            remember_catalog_entity(
                self._server,
                user_scope_id="global",
                query=name,
                entity_type="artist",
                item=updated,
                confidence=0.98,
                event_weight=0.0,
                event_type="artist_metadata",
                source="home_radio_artwork_worker",
                learn_query_alias=False,
            )
        return True
