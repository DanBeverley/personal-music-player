from __future__ import annotations

from copy import copy
from dataclasses import dataclass, field, is_dataclass, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence
import hashlib
import re
import time
import unicodedata


QUEUE_TARGET_DEPTH = 2
CANDIDATE_SNAPSHOT_TTL_SECONDS = 8 * 60 * 60
CANDIDATE_REFRESH_RETRY_SECONDS = 30 * 60
RADIO_SNAPSHOT_TTL_SECONDS = 8 * 60 * 60
READY_ARTIFACT_TTL_SECONDS = 8 * 60 * 60
ACTIVE_ARTIFACT_TTL_SECONDS = 24 * 60 * 60
# A previously accepted active feed may remain visible while its replacement
# is being rebuilt. It is never promotable or counted toward ready depth.
STALE_ACTIVE_FALLBACK_TTL_SECONDS = 3 * 24 * 60 * 60
HOME_DISCOVERY_EXPOSURE_COOLDOWN_SECONDS = 24 * 60 * 60
ALBUM_UNEXPOSED_ARTIST_RESERVE_TARGET = 16
SUPPLY_IDLE_PROGRESS_RECHECK_SECONDS = 5 * 60
SUPPLY_IDLE_NO_PROGRESS_RECHECK_SECONDS = 30 * 60


def normalize_artist_token(value: Any) -> str:
    """Normalize display names for cross-provider exposure comparisons."""

    normalized = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", normalized).split())


def artist_identity_aliases(value: Mapping[str, Any] | str | None) -> set[str]:
    """Return stable identity and name aliases for one artist-bearing value."""

    aliases: set[str] = set()

    def add(raw_value: Any, *, prefix: str = "") -> None:
        raw = " ".join(str(raw_value or "").strip().casefold().split())
        if not raw:
            return
        if prefix:
            raw = f"{prefix}{raw.removeprefix(prefix)}"
        aliases.add(raw)
        if raw.startswith("artist:"):
            name = raw.removeprefix("artist:")
            token = normalize_artist_token(name)
            if token:
                aliases.update({token, f"artist:{token}"})
        elif not raw.startswith(
            ("musicbrainz:artist:", "provider:artist:", "mix:")
        ):
            token = normalize_artist_token(raw)
            if token:
                aliases.update({token, f"artist:{token}"})

    if isinstance(value, str):
        add(value)
        return aliases
    if not isinstance(value, Mapping):
        return aliases
    for key in ("artist_name", "artist", "name", "seed_artist_key"):
        add(value.get(key))
    add(value.get("seed_artist_identity"))
    add(value.get("canonical_artist_id"), prefix="musicbrainz:artist:")
    add(value.get("musicbrainz_artist_id"), prefix="musicbrainz:artist:")
    add(value.get("artist_mbid"), prefix="musicbrainz:artist:")
    add(value.get("provider_artist_id"), prefix="provider:artist:")
    for identity in value.get("seed_artist_identities") or []:
        add(identity)
    for entity in value.get("artist_entities") or []:
        if isinstance(entity, Mapping):
            aliases.update(artist_identity_aliases(entity))
    return aliases


def home_supply_profile_fingerprint(user_scope_id: str) -> str:
    """Return the stable account-owned identity of durable Home supply.

    Request/profile fingerprints contain rotating launch, history, and refresh
    inputs.  Those values belong to ranking, not storage ownership; using them
    as inventory keys split one account's enrichment into competing stores.
    """

    scope = str(user_scope_id or "guest").strip() or "guest"
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24]
    return f"home-supply-v1:{digest}"


def with_home_supply_profile(value: Any) -> Any:
    """Copy a taste object onto the stable supply identity."""

    profile_key = home_supply_profile_fingerprint(
        str(getattr(value, "user_scope_id", "") or "guest")
    )
    if is_dataclass(value):
        return replace(value, profile_key=profile_key)
    cloned = copy(value)
    setattr(cloned, "profile_key", profile_key)
    return cloned


class RowRequirement(str, Enum):
    """How a row participates in feed acceptance.

    ``core`` rows are always required. ``earned`` rows are required only when
    the caller reports enough user evidence. ``independent`` rows use their
    own supply lifecycle and are optional at the feed level; when present,
    their row-specific minimum and quality checks remain strict.
    """

    CORE = "core"
    EARNED = "earned"
    INDEPENDENT = "independent"


class HomeJobType(str, Enum):
    RECONCILE_INTERACTION = "reconcile_interaction"
    REFRESH_CANDIDATES = "refresh_candidates"
    REFRESH_RADIO = "refresh_radio"
    REPAIR_ARTWORK = "repair_artwork"
    COMPOSE_FEED = "compose_feed"


class HomeJobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RowContract:
    kind: str
    requirement: RowRequirement
    min_items: int
    target_items: int
    max_items: int
    allow_reuse: bool = False
    max_reuse_age_seconds: int = 0

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("row kind is required")
        if not 0 <= self.min_items <= self.target_items <= self.max_items:
            raise ValueError(f"invalid item bounds for {self.kind}")
        if self.allow_reuse and self.max_reuse_age_seconds <= 0:
            raise ValueError(f"reusable row {self.kind} requires a positive max age")


@dataclass(frozen=True)
class PopularRadioContract:
    min_cards: int = 10
    target_cards: int = 12
    max_cards: int = 15
    min_tracks_per_card: int = 8
    target_tracks_per_card: int = 12
    min_discovery_cards: int = 3
    min_mix_cards: int = 2
    min_new_artist_identities: int = 3
    verified_artwork_prefix: str = "/artist_artwork/"


HOME_ROW_CONTRACTS: tuple[RowContract, ...] = (
    RowContract("todays_pick", RowRequirement.CORE, 6, 6, 8),
    RowContract(
        "featured_new_albums",
        RowRequirement.INDEPENDENT,
        4,
        8,
        10,
        allow_reuse=True,
        max_reuse_age_seconds=24 * 60 * 60,
    ),
    RowContract("last_played", RowRequirement.EARNED, 8, 8, 16),
    RowContract("frequently_listened", RowRequirement.EARNED, 8, 8, 16),
    RowContract("made_for_you", RowRequirement.CORE, 5, 8, 12),
    RowContract("because_you_played", RowRequirement.EARNED, 12, 12, 48),
    RowContract(
        "popular_radio",
        RowRequirement.INDEPENDENT,
        10,
        12,
        15,
        allow_reuse=True,
        max_reuse_age_seconds=8 * 60 * 60,
    ),
    RowContract(
        "recommended_albums",
        RowRequirement.INDEPENDENT,
        4,
        12,
        48,
        allow_reuse=True,
        max_reuse_age_seconds=24 * 60 * 60,
    ),
    RowContract("recommended_artists", RowRequirement.CORE, 10, 10, 48),
    RowContract("quiet_picks", RowRequirement.CORE, 20, 20, 200),
)
POPULAR_RADIO_CONTRACT = PopularRadioContract()
HOME_ROW_ORDER = tuple(contract.kind for contract in HOME_ROW_CONTRACTS)
HOME_ROW_CONTRACT_BY_KIND = {contract.kind: contract for contract in HOME_ROW_CONTRACTS}


@dataclass(frozen=True)
class ContractReport:
    accepted: bool
    violations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def evaluate_feed_contract(
    rows: Sequence[Mapping[str, Any]],
    *,
    earned_row_kinds: Iterable[str] = (),
    reused_row_ages_seconds: Mapping[str, float] | None = None,
) -> ContractReport:
    """Evaluate the one final feed-level row contract.

    Row-specific semantic/playability checks remain inputs to the row builders.
    This function owns only presence, order, cardinality, and explicit reuse.
    """

    earned = {str(value).strip() for value in earned_row_kinds if str(value).strip()}
    reuse_ages = dict(reused_row_ages_seconds or {})
    kinds = [str(row.get("kind") or "").strip() for row in rows]
    violations: list[str] = []
    warnings: list[str] = []

    if any(not kind for kind in kinds):
        violations.append("row_kind_missing")
    if len(kinds) != len(set(kinds)):
        violations.append("duplicate_row_kind")
    unknown = [kind for kind in kinds if kind and kind not in HOME_ROW_CONTRACT_BY_KIND]
    if unknown:
        violations.extend(f"unknown_row:{kind}" for kind in unknown)

    order = {kind: index for index, kind in enumerate(HOME_ROW_ORDER)}
    known_kinds = [kind for kind in kinds if kind in order]
    if known_kinds != sorted(known_kinds, key=order.__getitem__):
        violations.append("row_order_invalid")

    row_by_kind = {str(row.get("kind") or "").strip(): row for row in rows}
    for contract in HOME_ROW_CONTRACTS:
        required = contract.requirement is RowRequirement.CORE or (
            contract.requirement is RowRequirement.EARNED and contract.kind in earned
        )
        row = row_by_kind.get(contract.kind)
        if row is None:
            if required:
                violations.append(f"required_row_missing:{contract.kind}")
            continue
        items = row.get("items")
        count = len(items) if isinstance(items, list) else 0
        if count < contract.min_items:
            violations.append(f"row_below_min:{contract.kind}:{count}/{contract.min_items}")
        if count > contract.max_items:
            violations.append(f"row_above_max:{contract.kind}:{count}/{contract.max_items}")
        if contract.kind in reuse_ages:
            age = max(float(reuse_ages[contract.kind] or 0.0), 0.0)
            if not contract.allow_reuse:
                violations.append(f"row_reuse_forbidden:{contract.kind}")
            elif age > contract.max_reuse_age_seconds:
                violations.append(f"row_reuse_expired:{contract.kind}")
            else:
                warnings.append(f"row_reused:{contract.kind}")

    return ContractReport(
        accepted=not violations,
        violations=tuple(dict.fromkeys(violations)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _radio_artist_identity(card: Mapping[str, Any]) -> str:
    return str(
        card.get("seed_artist_identity")
        or card.get("seed_artist_key")
        or card.get("id")
        or ""
    ).strip().casefold()


def _normalized_artist_aliases(card: Mapping[str, Any]) -> set[str]:
    aliases: set[str] = set()
    values = [
        card.get("seed_artist_identity"),
        card.get("seed_artist_key"),
        card.get("artist_name"),
        *(card.get("seed_artist_identities") or []),
    ]
    for value in values:
        normalized = " ".join(str(value or "").strip().casefold().split())
        if not normalized:
            continue
        aliases.add(normalized)
        if normalized.startswith("artist:"):
            aliases.add(normalized.removeprefix("artist:"))
        elif not normalized.startswith(
            ("provider:artist:", "musicbrainz:artist:", "mix:")
        ):
            aliases.add(f"artist:{normalized}")
    return aliases


def evaluate_popular_radio_contract(
    cards: Sequence[Mapping[str, Any]],
    *,
    previous_artist_identities: Iterable[str] = (),
    known_artist_identities: Iterable[str] = (),
) -> ContractReport:
    """Own all Popular Radio publication and rotation-novelty gates."""

    from ..discovery.radio_inventory import (
        _is_playable_radio_record, _radio_metadata_complete, _radio_source_key,
        _radio_mix_artwork_complete,
    )

    contract = POPULAR_RADIO_CONTRACT
    violations: list[str] = []
    if len(cards) < contract.min_cards:
        violations.append(f"radio_below_min_cards:{len(cards)}/{contract.min_cards}")
    if len(cards) > contract.max_cards:
        violations.append(f"radio_above_max_cards:{len(cards)}/{contract.max_cards}")

    identities: list[str] = []
    discovery_count = 0
    genuine_discovery_count = 0
    mix_count = 0
    mix_identities: set[str] = set()
    known: set[str] = set()
    for value in known_artist_identities:
        normalized = " ".join(str(value or "").strip().casefold().split())
        if not normalized:
            continue
        known.add(normalized)
        if normalized.startswith("artist:"):
            known.add(normalized.removeprefix("artist:"))
        elif not normalized.startswith(
            ("provider:artist:", "musicbrainz:artist:", "mix:")
        ):
            known.add(f"artist:{normalized}")
    for index, card in enumerate(cards):
        identity = _radio_artist_identity(card)
        if not identity:
            violations.append(f"radio_artist_identity_missing:{index}")
        else:
            identities.append(identity)
        thumbnail = str(card.get("thumbnail") or "").strip()
        collage = [str(value or "").strip() for value in card.get("collage_images") or []]
        if not thumbnail.startswith(contract.verified_artwork_prefix) or not any(
            value.startswith(contract.verified_artwork_prefix) for value in collage
        ):
            violations.append(f"radio_artwork_unverified:{identity or index}")
        if str(card.get("radio_mode") or "").strip().casefold() in {"multi_artist_mix", "mix", "artist_mix"}:
            if not _radio_mix_artwork_complete(card):
                violations.append(f"radio_mix_sources_unverified:{identity or index}")
        else:
            from ..storage.artist_artwork import artist_artwork_token
            if thumbnail != f"/artist_artwork/{artist_artwork_token(identity)}":
                violations.append(f"radio_artwork_owner_mismatch:{identity or index}")
        tracks = card.get("tracks") or card.get("items")
        track_count = len(tracks) if isinstance(tracks, list) else 0
        if track_count < contract.min_tracks_per_card:
            violations.append(
                f"radio_track_depth:{identity or index}:{track_count}/{contract.min_tracks_per_card}"
            )
        # Revalidate retained reservoirs and persisted cards at publication,
        # not just the newly acquired tracks used to create a rotation.
        valid_tracks = [track for track in tracks or [] if isinstance(track, Mapping)]
        if len(valid_tracks) != track_count or any(
            not _radio_metadata_complete(track) or not _is_playable_radio_record(track)
            for track in valid_tracks
        ):
            violations.append(f"radio_track_metadata_incomplete:{identity or index}")
        if len({_radio_source_key(track) for track in valid_tracks}) != len(valid_tracks):
            violations.append(f"radio_track_source_duplicate:{identity or index}")
        affinity_is_discovery = str(
            card.get("seed_affinity") or ""
        ).strip().casefold() in {
            "related",
            "exploratory",
            "similar",
        }
        is_mix = str(
            card.get("radio_mode") or card.get("mode") or ""
        ).strip().casefold() in {"multi_artist_mix", "mix", "artist_mix"}
        # Mixes have their own contract. Discovery must be carried by
        # independently playable artist cards, otherwise two familiar mixes
        # can falsely satisfy the exploration requirement.
        if affinity_is_discovery and not is_mix:
            discovery_count += 1
            if not known or not (_normalized_artist_aliases(card) & known):
                genuine_discovery_count += 1
        if is_mix:
            mix_count += 1
            mix_identities.update(
                str(value).strip().casefold()
                for value in (card.get("seed_artist_identities") or card.get("artist_identities") or [])
                if str(value).strip()
            )
    if len(identities) != len(set(identities)):
        violations.append("radio_artist_identity_duplicate")
    if discovery_count < contract.min_discovery_cards:
        violations.append(
            f"radio_discovery_shortage:{discovery_count}/{contract.min_discovery_cards}"
        )
    if known and genuine_discovery_count < contract.min_discovery_cards:
        violations.append(
            "radio_user_discovery_shortage:"
            f"{genuine_discovery_count}/{contract.min_discovery_cards}"
        )
    if mix_count < contract.min_mix_cards:
        violations.append(f"radio_mix_shortage:{mix_count}/{contract.min_mix_cards}")
    if mix_count and len(mix_identities) < mix_count * 2:
        violations.append("radio_mix_identity_shortage")

    previous = {
        str(value).strip().casefold()
        for value in previous_artist_identities
        if str(value).strip()
    }
    if previous:
        actual_identities = {
            source.casefold()
            for card in cards
            for source in (card.get("seed_artist_identities") or [])
        } | {
            identity for identity, card in zip(identities, cards)
            if str(card.get("radio_mode") or "").strip().casefold() not in {"multi_artist_mix", "mix", "artist_mix"}
        }
        novel_count = len(actual_identities - previous)
        if novel_count < contract.min_new_artist_identities:
            violations.append(
                f"radio_rotation_novelty_shortage:{novel_count}/{contract.min_new_artist_identities}"
            )
    return ContractReport(
        accepted=not violations,
        violations=tuple(dict.fromkeys(violations)),
    )


def retry_delay_seconds(attempt: int) -> int:
    """One bounded backoff contract shared by every Home job type."""

    normalized = max(int(attempt or 0), 1)
    return min(5 * (2 ** (normalized - 1)), 300)


@dataclass(frozen=True)
class HomeStateRecord:
    user_scope_id: str
    active_artifact_id: str = ""
    ready_artifact_ids: tuple[str, ...] = ()
    active_version: int = 0
    revision: int = 0
    last_launch_token: str = ""
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.user_scope_id.strip():
            raise ValueError("user_scope_id is required")
        if len(self.ready_artifact_ids) > QUEUE_TARGET_DEPTH:
            raise ValueError("ready queue exceeds target depth")
        if len(self.ready_artifact_ids) != len(set(self.ready_artifact_ids)):
            raise ValueError("ready queue contains duplicates")
        if self.active_artifact_id and self.active_artifact_id in self.ready_artifact_ids:
            raise ValueError("active artifact cannot also be ready")


@dataclass(frozen=True)
class HomeJobRecord:
    job_id: str
    user_scope_id: str
    job_type: HomeJobType
    generation_key: str
    status: HomeJobStatus = HomeJobStatus.PENDING
    priority: int = 0
    attempts: int = 0
    max_attempts: int = 5
    run_after: float = 0.0
    lease_token: str = ""
    lease_owner: str = ""
    lease_expires_at: float = 0.0
    payload: Mapping[str, Any] = field(default_factory=dict)
    last_error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class CandidateSnapshotRecord:
    snapshot_id: str
    user_scope_id: str
    profile_fingerprint: str
    generated_at: float
    expires_at: float
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.snapshot_id.strip() or not self.user_scope_id.strip():
            raise ValueError("snapshot_id and user_scope_id are required")
        if self.expires_at <= self.generated_at:
            raise ValueError("candidate snapshot must expire after generation")
        if self.expires_at - self.generated_at > CANDIDATE_SNAPSHOT_TTL_SECONDS:
            raise ValueError("candidate snapshot exceeds the eight-hour TTL")

    def is_fresh(self, now: float | None = None) -> bool:
        at = time.time() if now is None else float(now)
        return self.generated_at <= at < self.expires_at

    def is_usable(self, now: float | None = None) -> bool:
        """Return whether the immutable verified facts may still be reused."""

        at = time.time() if now is None else float(now)
        return self.generated_at <= at


@dataclass(frozen=True)
class RadioSnapshotRecord:
    snapshot_id: str
    user_scope_id: str
    profile_fingerprint: str
    generated_at: float
    expires_at: float
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.snapshot_id.strip() or not self.user_scope_id.strip():
            raise ValueError("snapshot_id and user_scope_id are required")
        if self.expires_at <= self.generated_at:
            raise ValueError("radio snapshot must expire after generation")
        if self.expires_at - self.generated_at > RADIO_SNAPSHOT_TTL_SECONDS:
            raise ValueError("radio snapshot exceeds the eight-hour TTL")

    def is_fresh(self, now: float | None = None) -> bool:
        at = time.time() if now is None else float(now)
        return self.generated_at <= at < self.expires_at


@dataclass(frozen=True)
class FeedArtifactRecord:
    artifact_id: str
    user_scope_id: str
    profile_fingerprint: str
    candidate_snapshot_id: str
    radio_snapshot_id: str
    created_at: float
    expires_at: float
    payload: Mapping[str, Any]
    allocation_epoch: int = 0

    def __post_init__(self) -> None:
        if not self.artifact_id.strip() or not self.user_scope_id.strip():
            raise ValueError("artifact_id and user_scope_id are required")
        if not self.candidate_snapshot_id.strip():
            raise ValueError("candidate_snapshot_id is required")
        if self.allocation_epoch < 0:
            raise ValueError("allocation_epoch cannot be negative")
        if self.expires_at <= self.created_at:
            raise ValueError("feed artifact must expire after creation")
        if self.expires_at - self.created_at > ACTIVE_ARTIFACT_TTL_SECONDS:
            raise ValueError("feed artifact exceeds the active-feed TTL")

    def is_servable(self, now: float | None = None) -> bool:
        at = time.time() if now is None else float(now)
        return self.created_at <= at < self.expires_at

    def is_promotable(self, now: float | None = None) -> bool:
        at = time.time() if now is None else float(now)
        return self.is_servable(at) and at - self.created_at <= READY_ARTIFACT_TTL_SECONDS
