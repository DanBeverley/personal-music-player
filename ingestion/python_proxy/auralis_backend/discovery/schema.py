from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, Dict, List


JsonDict = Dict[str, Any]


_composition_facts = ContextVar('home_composition_facts', default=None)


@contextmanager
def composition_facts():
    """Reuse immutable profile facts only for this composition, never globally."""
    token = _composition_facts.set({})
    try:
        yield
    finally:
        _composition_facts.reset(token)


def composition_fact(function):
    @wraps(function)
    def read(taste, *keys):
        facts = _composition_facts.get()
        if facts is None:
            return function(taste, *keys)
        key = (function, id(taste), keys)
        if key not in facts:
            # Keep the profile alive to prevent id reuse inside this scope.
            facts[key] = (taste, function(taste, *keys))
        return facts[key][1]
    return read


@dataclass(frozen=True)
class RowRecipe:
    kind: str
    title: str
    item_type: str
    launch_required: bool
    min_items: int
    target_items: int
    max_items: int
    page_size: int
    can_page: bool
    candidate_sources: tuple[str, ...]
    ranking_intent: str
    fallback_policy: str = "drop_if_weak"
    row_style: str = ""


@dataclass(frozen=True)
class LaneRecipe:
    lane_id: str
    title: str
    min_items: int
    target_items: int
    positive_hints: tuple[str, ...] = ()
    negative_hints: tuple[str, ...] = ()
    allow_acoustic: bool = True
    candidate_sources: tuple[str, ...] = ()


@dataclass
class TasteProfile:
    user_scope_id: str
    profile_key: str
    signal_tier: str
    recent_tracks: List[JsonDict] = field(default_factory=list)
    top_tracks: List[JsonDict] = field(default_factory=list)
    last_played_tracks: List[JsonDict] = field(default_factory=list)
    anchor_tracks: List[JsonDict] = field(default_factory=list)
    full_history_tracks: List[JsonDict] = field(default_factory=list)
    frequent_tracks: List[JsonDict] = field(default_factory=list)
    artist_hints: List[str] = field(default_factory=list)
    album_hints: List[str] = field(default_factory=list)
    top_artists: List[str] = field(default_factory=list)
    listened_artists: List[str] = field(default_factory=list)
    top_albums: List[str] = field(default_factory=list)
    recent_queries: List[str] = field(default_factory=list)
    taste_queries: List[str] = field(default_factory=list)
    collaborative_track_ids: List[str] = field(default_factory=list)
    avoid_ids: List[str] = field(default_factory=list)
    # Identities visible in active/prepared feeds. Track selection now treats
    # these as exposure preferences, balancing useful familiar and unseen
    # supply. Explicit row/queue exclusions and promotion novelty stay separate.
    hard_avoid_ids: List[str] = field(default_factory=list)
    force_refresh: bool = False
    refresh_token: str = ""
    rotation_epoch: int = 0
    source_profile: JsonDict = field(default_factory=dict)
    taste_mode: str = "neatie"
    listenbrainz_username: str = ""

    @property
    def is_cold_start(self) -> bool:
        if self.signal_tier == "cold_start":
            return True
        return not any(
            (
                self.recent_tracks,
                self.top_tracks,
                self.last_played_tracks,
                self.anchor_tracks,
                self.artist_hints,
                self.top_artists,
                self.listened_artists,
                self.taste_queries,
            )
        )


@dataclass
class DiscoveryCandidate:
    item: JsonDict
    source: str
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    item_type: str = "track"


@dataclass
class DiscoveryRow:
    id: str
    title: str
    kind: str
    item_type: str
    items: List[JsonDict]
    row_style: str = ""
    meta: JsonDict = field(default_factory=dict)
    next_offset: int = 0
    has_more: bool = False


@dataclass
class DiscoveryArtifact:
    session_id: str
    user_scope_id: str
    profile_key: str
    generated_at: float
    expires_at: float
    rows: List[DiscoveryRow]
    diagnostics: JsonDict
    candidate_pool_counts: Dict[str, int]
    provider_timings_ms: Dict[str, int]
    home_tab_lanes: Dict[str, JsonDict]
    accepted: bool
    quality_reasons: List[str] = field(default_factory=list)
    artifact_source: str = "fresh_build"
