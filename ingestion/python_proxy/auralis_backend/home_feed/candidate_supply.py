from __future__ import annotations

from dataclasses import dataclass, replace
from collections import Counter
from typing import Any, Mapping
import time
import uuid

from ..discovery.enrichment import (
    MaterializedCandidateSupply,
    build_enrichment_plan,
    complete_inventory_release_metadata,
    materialize_enrichment_plan,
    profile_artist_demand,
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
    recommendation_track_identities,
    canonical_item_identity,
    rebuild_album_views, ALBUM_VIEW_POOLS,
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
from ..discovery.schema import DiscoveryCandidate, TasteProfile, composition_facts
from ..discovery.ranking import _album_taste_relation, _history_track_ids, _taste_artist_keys, artist_name
from ..domain.catalog import normalize_artist_name


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
    """Reserve accounting includes exposure; acquisition eligibility does not."""
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
    for field in ('full_history_tracks', 'frequent_tracks', 'recent_tracks',
                  'top_tracks', 'last_played_tracks'):
        for track in getattr(taste, field, []) or []:
            aliases.update(artist_identity_aliases(track))
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
    with composition_facts():
        return _assess_album_reserve(inventory, repositories=repositories, taste=taste)


def _album_artist_evidence(inventory, taste):
    """Keep the strongest evidenced edge per artist identity, not first row."""
    evidence = {}
    history_ids = _history_track_ids(taste)
    taste_keys = {normalize_artist_name(name) for name in _taste_artist_keys(taste)}
    for source in ('artist_graph', 'similarity', 'genre_mood', 'collaborative',
                   'popularity', 'discovery_universe'):
        for candidate in inventory.pools.get(source) or []:
            item = dict(candidate.item)
            name = str(item.get('artist') or item.get('artist_name') or
                       item.get('name') or item.get('neighbor_artist') or '').strip()
            token = normalize_artist_name(name)
            if not token or item.get('source_authority') == 'search_only':
                continue
            # Track/catalog adapters already carry artist-owned IDs outside
            # provider_artist_id. Keep that evidence instead of resolving the
            # same name again, where legitimate homonyms are ambiguous.
            provider_ids = {str(item.get(field) or '').strip() for field in
                            ('provider_artist_id', 'artist_id', 'artistId')
                            if str(item.get(field) or '').startswith('UC')}
            if candidate.item_type == 'artist':
                provider_ids.update(str(item.get(field) or '') for field in ('id', 'browseId')
                                    if str(item.get(field) or '').startswith('UC'))
            for credit in item.get('artists') or []:
                if isinstance(credit, dict) and normalize_artist_name(credit.get('name') or credit.get('artist')) == token:
                    provider_id = str(credit.get('id') or credit.get('browseId') or '')
                    if provider_id.startswith('UC'):
                        provider_ids.add(provider_id)
            if len(provider_ids) > 1:
                continue  # Conflicting ownership is not a name-resolution hint.
            if provider_ids:
                item['provider_artist_id'] = next(iter(provider_ids))
            allowed, relation, strength = _album_taste_relation(
                candidate, item, taste, history_ids=history_ids)
            parent = str(item.get('related_to_artist') or '').strip()
            relationship_score = float(item.get('relationship_score') or 0)
            if (not allowed and normalize_artist_name(parent) in taste_keys
                    and relationship_score >= .6):
                allowed, relation, strength = True, 'artist_graph', relationship_score
            if not allowed and not taste.is_cold_start:
                continue
            ids = frozenset(alias for alias in artist_identity_aliases(item)
                            if alias.startswith(('provider:artist:', 'musicbrainz:artist:')))
            key = (token, ids)
            record = {field: item[field] for field in (
                'provider_artist_id', 'musicbrainz_artist_id', 'canonical_artist_id',
                'genres', 'discovery_genres', 'related_to_artist',
                'relationship_provider', 'relationship_evidence') if item.get(field)}
            record.update(artist=name, artist_graph_keys=list(dict.fromkeys([
                *(item.get('artist_graph_keys') or []), *([parent] if parent else [])])),
                relation_strength=min(strength, 1.0), album_relation_origin=relation)
            if key not in evidence or strength > evidence[key][0]:
                evidence[key] = (strength, record)
    # Acquisition retains graph edges separately from playable candidates.
    # Recheck the parent against this profile before using an edge as a seed.
    for progress in (inventory.acquisition_ledger.get('request_progress') or {}).values():
        if not isinstance(progress, dict):
            continue
        for edge in progress.get('artist_edges') or []:
            if not isinstance(edge, dict):
                continue
            name = str(edge.get('artist') or '').strip()
            token = normalize_artist_name(name)
            parent = str(edge.get('related_to_artist') or '').strip()
            try:
                strength = float(edge.get('relationship_score') or 0)
            except (TypeError, ValueError):
                continue
            if (not token or normalize_artist_name(parent) not in taste_keys
                    or not .6 <= strength <= 1):
                continue
            record = {field: edge[field] for field in (
                'musicbrainz_artist_id', 'related_to_artist',
                'relationship_provider', 'relationship_evidence') if edge.get(field)}
            record.update(artist=name, artist_graph_keys=[parent],
                          relation_strength=strength, album_relation_origin='artist_graph')
            ids = frozenset(alias for alias in artist_identity_aliases(record)
                            if alias.startswith(('provider:artist:', 'musicbrainz:artist:')))
            key = (token, ids)
            if key not in evidence or strength > evidence[key][0]:
                evidence[key] = (strength, record)
    # Relationship strength and provider identity can arrive on different
    # records. A stronger name-only graph edge must not erase a unique owned
    # track credit; conflicting canonical identities remain separate.
    by_name = {}
    for (token, ids), (_strength, record) in evidence.items():
        provider_ids, mbids = by_name.setdefault(token, (set(), set()))
        if str(record.get('provider_artist_id') or '').startswith('UC'):
            provider_ids.add(record['provider_artist_id'])
        mbids.update(identity for identity in ids if identity.startswith('musicbrainz:artist:'))
    merged = {}
    for (token, ids), (strength, record) in evidence.items():
        provider_ids, mbids = by_name[token]
        if not record.get('provider_artist_id') and len(provider_ids) == 1 and len(mbids) <= 1:
            record = {**record, 'provider_artist_id': next(iter(provider_ids))}
            ids = ids | frozenset(alias for alias in artist_identity_aliases(record)
                                  if alias.startswith(('provider:artist:', 'musicbrainz:artist:')))
        key = (token, ids)
        if key not in merged or strength > merged[key][0]:
            merged[key] = (strength, record)
    return merged


def _with_album_relationship(item, evidence):
    token = normalize_artist_name(artist_name(item))
    identities = artist_identity_aliases(item)
    matches = []
    for (name, ids), (strength, record) in evidence.items():
        if name != token:
            continue
        # A matching name cannot override conflicting canonical ownership.
        if any((left := {i for i in identities if i.startswith(prefix)})
               and (right := {i for i in ids if i.startswith(prefix)})
               and left.isdisjoint(right)
               for prefix in ('provider:artist:', 'musicbrainz:artist:')):
            continue
        matches.append((bool(identities & ids), strength, record))
    if not matches:
        return item
    matches.sort(key=lambda value: (value[0], value[1]), reverse=True)
    # Ambiguous same-name identities without an identity match stay unresolved.
    if not matches[0][0] and len({frozenset(alias for alias in artist_identity_aliases(r)
                                           if alias.startswith(('provider:artist:', 'musicbrainz:artist:')))
                                 for _, _, r in matches}) > 1:
        return item
    record = matches[0][2]
    output = dict(item)
    for field in ('related_to_artist', 'relationship_provider', 'relationship_evidence',
                  'album_relation_origin'):
        if record.get(field):
            output[field] = record[field]
    for field in ('artist_graph_keys', 'genres', 'discovery_genres'):
        output[field] = list(dict.fromkeys([*(item.get(field) or []), *(record.get(field) or [])]))
    output['relation_strength'] = max(float(item.get('relation_strength') or 0),
                                      float(record.get('relation_strength') or 0))
    return output


def _pending_album_metadata(inventory, raw, verified, *, taste=None):
    """Retain useful artist frontiers before truncating the bounded backlog."""
    checked = {canonical_item_identity(item, item_type='album'): item for item in verified}
    pending = {canonical_item_identity(item, item_type='album'): dict(item) for item in raw
               if checked.get(canonical_item_identity(item, item_type='album'), {}).get('playable') is not True}
    if not isinstance(taste, TasteProfile):
        return [{**item, 'playable': False} for item in list(pending.values())[:160]]
    with composition_facts():
        history_ids = _history_track_ids(taste)
        evidence = _album_artist_evidence(inventory, taste)
        familiar = {normalize_artist_name(name) for name in _taste_artist_keys(taste)}
        familiar.update(normalize_artist_name(artist_name(item))
                        for item in [*taste.full_history_tracks, *taste.frequent_tracks])
        counts = Counter(normalize_artist_name(artist_name(c.item))
                         for c in inventory.pools.get('album') or [] if c.item.get('playable') is True)
        tiers = ({}, {}, {})
        for identity, item in pending.items():
            item = _with_album_relationship(item, evidence)
            artist = normalize_artist_name(artist_name(item))
            related = taste.is_cold_start or _album_taste_relation(
                DiscoveryCandidate(item, 'album', item_type='album'), dict(item), taste,
                history_ids=history_ids)[0]
            tier = 0 if related and artist not in familiar else 1 if related else 2
            total = max(len(item.get('canonical_tracks') or []), int(item.get('canonical_track_count') or 0))
            required = total if total < 6 else max(6, int(total * .8 + .999))
            proved = len(checked.get(identity, {}).get('tracks') or checked.get(identity, {}).get('canonical_tracks') or [])
            rank = (counts[artist], max(required - proved, 0), identity)
            tiers[tier].setdefault(artist, []).append((rank, {**item, 'playable': False}))
        output = []
        for groups in tiers:
            ordered = sorted((sorted(items, key=lambda value: value[0]) for items in groups.values()),
                             key=lambda values: values[0][0])
            # One album per artist per round; old catalogs cannot occupy all
            # pending slots ahead of a newly acquired neighboring artist.
            for index in range(max((len(items) for items in ordered), default=0)):
                for items in ordered:
                    if index < len(items):
                        output.append(items[index][1])
                        if len(output) == 160:
                            return output
        return output


def _assess_album_reserve(inventory, *, repositories, taste):
    known = _known_artist_aliases(repositories, taste)
    qualified: dict[str, set[str]] = {}
    qualified_album_ids: dict[str, set[str]] = {}
    assessed = isinstance(taste, TasteProfile)
    related_keys: set[str] = set()
    relation_rejections: dict[str, int] = {}
    if assessed:
        evidence = _album_artist_evidence(inventory, taste)
        albums = [replace(candidate, item=_with_album_relationship(candidate.item, evidence))
                  for candidate in inventory.pools.get('album') or []]
        ledger = dict(inventory.acquisition_ledger or {})
        ledger['album_verification_backlog'] = [
            _with_album_relationship(item, evidence)
            for item in ledger.get('album_verification_backlog') or []]
        inventory = rebuild_album_views(replace(inventory,
            pools={**inventory.pools, 'album': albums}, acquisition_ledger=ledger))
        with composition_facts():
            history_ids = _history_track_ids(taste)
            for candidate in inventory.pools.get('album', []) or []:
                item = candidate.item
                if candidate.item_type != 'album' or item.get('playable') is not True:
                    continue
                admitted, reason, _ = _album_taste_relation(candidate, dict(item), taste, history_ids=history_ids)
                if admitted or taste.is_cold_start:
                    related_keys.add(_album_artist_key(item))
                else:
                    relation_rejections[reason] = relation_rejections.get(reason, 0) + 1
    else:
        # The lightweight API status view reuses the worker's admission
        # assessment; it never loads a profile or performs provider work.
        prior = inventory.acquisition_ledger or {}
        if prior.get('album_profile_assessed'):
            related_keys = set(prior.get('album_profile_related_artist_keys') or [])
            assessed = True
    for candidate in inventory.pools.get("album", []) or []:
        if candidate.item_type != "album" or candidate.item.get("playable") is not True:
            continue
        item = dict(candidate.item or {})
        key = _album_artist_key(item)
        if assessed and key not in related_keys:
            continue
        if key:
            qualified.setdefault(key, set()).update(artist_identity_aliases(item))
            name = normalize_artist_name(artist_name(item))
            qualified_album_ids.setdefault(name,set()).add(canonical_item_identity(item,item_type='album'))
    unexposed = {
        key for key, aliases in qualified.items() if not (aliases & known)
    }
    deficit = max(
        ALBUM_UNEXPOSED_ARTIST_RESERVE_TARGET - len(unexposed),
        0,
    )
    discovery_seeds = list(inventory.acquisition_ledger.get('album_priority_artist_seeds') or [])
    seed_evidence = dict(inventory.acquisition_ledger.get('album_artist_evidence') or {})
    if isinstance(taste, TasteProfile):
        discovery_seeds, seed_evidence = [], {}
        familiar = {normalize_artist_name(name) for name in _taste_artist_keys(taste)}
        familiar.update(normalize_artist_name(artist_name(track)) for track in
                        [*taste.full_history_tracks, *taste.frequent_tracks])
        # Exposure can prefer a seed; it cannot erase its catalog frontier.
        frontier = sorted(evidence.items(), key=lambda pair: (
            len(qualified_album_ids.get(pair[0][0], ())),
            bool(artist_identity_aliases(pair[1][1]) & known),
            -pair[1][0], pair[0][0], sorted(pair[0][1])))
        for (token, _ids), (_strength, record) in frontier:
            if token in familiar or token in seed_evidence:
                continue
            seed_evidence[token] = {k:v for k,v in record.items() if k != 'artist'}
            discovery_seeds.append(record['artist'])
            if len(discovery_seeds) == 32:
                break
    ledger = dict(inventory.acquisition_ledger or {})
    if assessed:
        ledger.update(album_profile_assessed=True,
                      album_profile_related_artist_keys=sorted(related_keys))
        if isinstance(taste, TasteProfile):
            ledger['album_relation_rejection_counts'] = relation_rejections
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
            'album_artist_counts': {key:len(values-{''}) for key,values in qualified_album_ids.items() if key},
            "album_unexposed_artist_count": len(unexposed),
            "album_unexposed_artist_target": ALBUM_UNEXPOSED_ARTIST_RESERVE_TARGET,
            "album_unexposed_artist_deficit": deficit,
            "album_priority_artist_seeds": discovery_seeds,
            'album_artist_evidence': seed_evidence,
            'album_expansion_artist_seeds': discovery_seeds,
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
            "unexposed_track_deficit": int((inventory.acquisition_ledger or {}).get('unexposed_track_deficit') or 0),
            "expansion_needed": bool(status.get("expansion_needed") or deficit > 0
                                     or (inventory.acquisition_ledger or {}).get('unexposed_track_deficit')
                                     or (inventory.acquisition_ledger or {}).get('profile_artist_demand_count')),
            "profile_artist_demand_count": int((inventory.acquisition_ledger or {}).get('profile_artist_demand_count') or 0),
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
    exposed = repositories.exposures.recent_track_exposures(
        user_scope_id, since=time.time() - 7 * 24 * 60 * 60)
    available = set()
    for source, values in inventory.pools.items():
        if source in {'history', 'profile_spine'}:
            continue
        for candidate in values:
            if candidate.item_type != 'track' or candidate.item.get('playable') is False:
                continue
            aliases = recommendation_track_identities(candidate.item)
            if aliases and not aliases & exposed.keys():
                available.add(canonical_item_identity(candidate.item, item_type='track'))
    status = _exposure_aware_supply_status(evaluated)
    # Count unoffered recordings, not a large inventory of exhausted facts.
    status.update(unexposed_track_count=len(available), unexposed_track_target=144,
                  unexposed_track_deficit=max(144-len(available), 0))
    status['expansion_needed'] = status['expansion_needed'] or len(available) < 144
    return status


def _general_candidate_content_hash(inventory: CandidateInventory | None) -> str:
    """Hash only supply owned by the general candidate worker.

    Older snapshots may still contain radio pools. Revalidation deliberately
    detaches those pools because radio has its own repository; that ownership
    cleanup is not a rewrite of the immutable general-candidate generation.
    """

    if inventory is None:
        return candidate_inventory_content_hash(None)
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
        self._consolidation_scans: dict[str, tuple[str, tuple[str, ...]]] = {}

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
        recent_ids = self._repositories.candidates.recent_ids(taste.user_scope_id, limit=12)
        ledger = dict(current.acquisition_ledger or {}) if current is not None else {}
        scan_key = (current.generation_id, recent_ids) if current is not None else None
        scan_completed = (
            current is not None
            and current.profile_fingerprint == stable_profile
            and (
                self._consolidation_scans.get(taste.user_scope_id) == scan_key
                or (ledger.get("consolidation_generation_id") == current.generation_id
                    and set(recent_ids) - {current.generation_id}
                        <= set(ledger.get("consolidation_fragment_ids") or []))
            )
        )
        # Only the historical scan is skipped. Current album/source admission
        # and coverage checks below still run, including revoked proofs.
        seen: set[str] = set(recent_ids) | {current.generation_id} if scan_completed else set()
        consolidated = current if scan_completed else None
        freshness_source = current if scan_completed else None
        pending_current = None if scan_completed else current
        album_facts: dict[str, dict[str, Any]] = {}

        def retain_album_facts(fragment):
            if fragment is None:
                return
            for source in ('album', *ALBUM_VIEW_POOLS):
                for candidate in fragment.pools.get(source) or []:
                    if candidate.item_type == 'album':
                        album_facts.setdefault(canonical_item_identity(candidate.item,item_type='album'),dict(candidate.item))
            for item in fragment.acquisition_ledger.get('album_verification_backlog') or []:
                album_facts.setdefault(canonical_item_identity(item,item_type='album'),dict(item))

        retain_album_facts(current)

        def merge_fragment(fragment):
            nonlocal consolidated, freshness_source
            if fragment is None or not fragment.generation_id or fragment.generation_id in seen:
                return
            seen.add(fragment.generation_id)
            fragment = replace(fragment, profile_fingerprint=stable_profile)
            retain_album_facts(fragment)
            if consolidated is None:
                consolidated = fragment
                freshness_source = fragment
            else:
                consolidated = merge_candidate_inventories(consolidated, fragment)

        records = () if scan_completed else self._repositories.candidates.iter_recent(taste.user_scope_id, limit=12)
        for record in records:
            inventory = candidate_inventory_from_payload(dict(record.payload or {}))
            if inventory is None:
                continue
            if pending_current is not None and (
                float(pending_current.generated_at or 0), pending_current.generation_id
            ) >= (float(inventory.generated_at or 0), inventory.generation_id):
                merge_fragment(pending_current)
                pending_current = None
            merge_fragment(inventory)
        merge_fragment(pending_current)
        if consolidated is None:
            return None, False
        with composition_facts():
            evidence = _album_artist_evidence(consolidated, taste)
            albums = [_with_album_relationship(item, evidence) for item in album_facts.values()]
        if albums:
            # Reuse the source verifier with no acquisition or artwork work.
            # Retained flags cannot bypass current edition/source proofs.
            verified = verify_materialized_supply(
                self._server,
                MaterializedCandidateSupply(pools={"album": albums}),
                taste,
                max_new_verifications=0,
                hydrate_artist_artwork=False,
            )
            consolidated = self._album_results(consolidated, albums, verified.pools.get('album') or [], taste=taste)
        consolidated = rebuild_album_views(consolidated)
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
            or _general_candidate_content_hash(current) != _general_candidate_content_hash(consolidated)
        )
        if not migration_required:
            current_checks = [c.item.get('source_recheck_after') for c in current.pools.get('album') or []]
            next_checks = [c.item.get('source_recheck_after') for c in consolidated.pools.get('album') or []]
            if (current_checks != next_checks or
                current.acquisition_ledger.get('album_verification_backlog') != consolidated.acquisition_ledger.get('album_verification_backlog')):
                # Proof clocks/backlog advance in the mutable owner without
                # rewriting an immutable feed snapshot or inventing a revision.
                metadata_update = replace(consolidated,generation_id=current.generation_id,
                    base_generation_id=current.base_generation_id,generated_at=current.generated_at,expires_at=current.expires_at)
                if store_candidate_inventory(self._server,metadata_update,expected_ready_generation_id=current.generation_id):
                    current = metadata_update
            if len(self._consolidation_scans) >= 64:
                self._consolidation_scans.pop(next(iter(self._consolidation_scans)))
            self._consolidation_scans[taste.user_scope_id] = scan_key
            return current, False
        now = time.time()
        migration_id = str(uuid.uuid4())
        migrated = replace(
            consolidated,
            profile_fingerprint=stable_profile,
            generation_id=migration_id,
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
                "profile_fragments_consolidated": len(seen),
                "consolidation_generation_id": migration_id,
                "consolidation_fragment_ids": sorted(seen),
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

    @staticmethod
    def _album_results(inventory, raw, verified, *, taste=None):
        ready = {canonical_item_identity(item,item_type='album'):item for item in verified if item.get('playable') is True}
        backlog = _pending_album_metadata(inventory, raw, verified, taste=taste)
        candidates = [DiscoveryCandidate(item=dict(item),source='album',score=4.2,item_type='album') for item in ready.values()]
        return rebuild_album_views(replace(inventory,pools={**inventory.pools,'album':candidates},
            acquisition_ledger={**inventory.acquisition_ledger,'album_verification_backlog':backlog}))

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
            exposure_status = exposure_aware_candidate_supply_status(
                current, repositories=self._repositories, user_scope_id=taste.user_scope_id)
            current = replace(current, acquisition_ledger={
                **dict(current.acquisition_ledger or {}),
                **{key: exposure_status[key] for key in
                   ('unexposed_track_count', 'unexposed_track_target', 'unexposed_track_deficit')},
            })
            if force and current.is_ready and exposure_status['unexposed_track_deficit']:
                current = candidate_inventory_refresh_seed(current, reason='recommendation_exposure_exhausted')
        now = time.time()
        artist_demand = profile_artist_demand(taste, current.acquisition_ledger if current else {})
        if (
            current is not None
            and current.is_ready
            and float(current.expires_at or 0.0) > now
            and not force
            and not artist_demand
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
        if current is not None and current.is_ready and consolidation_changed and not force and not artist_demand:
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
        verification_deferred = False
        proof_progress = False

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
            raw_albums = {}
            for item in [*((built.acquisition_ledger if built else {}).get('album_verification_backlog') or []),
                         *([c.item for c in (built.pools.get('album') or []) if float(c.item.get('source_recheck_after') or float('inf')) <= time.time()] if built else []),
                         *(supply.pools.get('album') or [])]:
                raw_albums[canonical_item_identity(item,item_type='album')] = item
            if raw_albums:
                supply = replace(supply,pools={**supply.pools,'album':list(raw_albums.values())})
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
            verification_deferred = bool(
                (verified.diagnostics or {}).get("source_verification_deferred")
            )
            proof_progress = int((verified.diagnostics or {}).get('source_verification_verified') or 0) > 0
            candidate = build_candidate_inventory(
                self._server,
                taste,
                previous=built,
                materialized_supply=verified,
            )
            if raw_albums:
                # Keep incomplete metadata outside ranked pools; source proofs
                # accumulate across bounded worker batches without refetching.
                candidate = replace(candidate,acquisition_ledger={**candidate.acquisition_ledger,
                    'album_verification_backlog':_pending_album_metadata(candidate,
                        list(raw_albums.values()), verified.pools.get('album') or [], taste=taste)})
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
            content_changed = _general_candidate_content_hash(candidate) != _general_candidate_content_hash(built)
            prior_no_progress = int(
                (built.acquisition_ledger if built is not None else {}).get(
                    "consecutive_no_progress_batches"
                )
                or 0
            )
            no_progress_count = (0 if added_ids or proof_progress else prior_no_progress
                                 if verification_deferred else prior_no_progress + 1)
            ledger = {
                **dict(candidate.acquisition_ledger or {}),
                "profile_artist_demand_count": len(profile_artist_demand(taste, candidate.acquisition_ledger)),
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
            if built.is_ready or verification_deferred:
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
        supply_status = exposure_aware_candidate_supply_status(
            built, repositories=self._repositories, user_scope_id=taste.user_scope_id)
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
            # Useful source proofs can advance before an album crosses its
            # minimum coverage. Keep its bounded job moving without claiming
            # new usable content, changing a snapshot or escalating backoff.
            retry_after_seconds=(30 if verification_deferred or (proof_progress and not made_progress)
                                 else retry_after if not made_progress else 0),
            retryable=verification_deferred or (not built.is_ready and (made_progress or proof_progress)),
            reason=(
                "candidate_verification_deferred"
                if verification_deferred
                else "candidate_proofs_advanced"
                if proof_progress and not made_progress
                else
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
