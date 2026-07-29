from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Set

from .candidates import artist_name, normalize_text, track_id
from .schema import DiscoveryCandidate, TasteProfile


TRUSTED_AUTHORITIES = {
    "official",
    "official_artist_channel",
    "topic",
    "label",
    "trusted_match",
    "canonical",
    "verified_catalog",
    "catalog_memory",
    "profile_verified",
}

GLOBAL_REGION_KEYS = {"global", "world", "international"}

DISCOVERY_ROWS = {
    "todays_pick",
    "made_for_you",
    "because_you_played",
    "popular_radio",
    "quiet_picks",
    "recommended_albums",
    "featured_new_albums",
    "home_lane",
    "personal_mix_slice",
}

EXPLORATORY_SOURCES = {
    "ytmusic_home",
    "popularity",
    "collaborative",
    "discovery_universe",
    "genre_mood",
    "lane_chill",
    "lane_workout",
    "lane_focus",
    "lane_mood",
}

BROAD_GLOBAL_PATHS = {"", "broad_global", "unproven"}

PERSONAL_PATH_ROWS = {
    "todays_pick",
    "made_for_you",
    "because_you_played",
    "popular_radio",
    "quiet_picks",
    "home_lane",
    "personal_mix_slice",
}


@dataclass(frozen=True)
class CompatibilityResult:
    allowed: bool
    score: float
    reason: str
    language: str
    region: str
    language_confidence: float
    region_confidence: float
    language_source: str
    region_source: str
    audience_profile: str = "general"
    audience_confidence: float = 0.0
    audience_source: str = "unknown"
    rejection_reason: str = ""

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "profile_compatibility_score": round(self.score, 4),
            "compatibility_reason": self.reason,
            "language": self.language,
            "region": self.region,
            "language_confidence": round(self.language_confidence, 4),
            "region_confidence": round(self.region_confidence, 4),
            "language_source": self.language_source,
            "region_source": self.region_source,
            "audience_profile": self.audience_profile,
            "audience_confidence": round(self.audience_confidence, 4),
            "audience_source": self.audience_source,
            "admission_rejection_reason": self.rejection_reason,
        }


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _profile_values(profile: Dict[str, Any], *keys: str) -> Set[str]:
    output: Set[str] = set()
    for key in keys:
        value = profile.get(key)
        if isinstance(value, dict):
            iterable: Iterable[Any] = value.keys()
        elif isinstance(value, (list, tuple, set)):
            iterable = value
        else:
            iterable = [value]
        for entry in iterable:
            normalized = normalize_text(entry)
            if normalized and normalized != "unknown":
                output.add(normalized)
    return output


def _track_language_values(taste: TasteProfile, key: str) -> Set[str]:
    output: Set[str] = set()
    for track in taste.recent_tracks + taste.top_tracks + taste.last_played_tracks:
        normalized = normalize_text(track.get(key))
        confidence = _number(track.get(f"{key}_confidence"))
        if normalized and normalized != "unknown" and confidence >= 0.45:
            output.add(normalized)
    return output


def accepted_languages(taste: TasteProfile) -> Set[str]:
    profile = taste.source_profile or {}
    values = _profile_values(
        profile,
        "accepted_languages",
        "supported_languages",
        "dominant_language",
        "languages",
    )
    return values | _track_language_values(taste, "language")


def accepted_regions(taste: TasteProfile) -> Set[str]:
    profile = taste.source_profile or {}
    values = _profile_values(
        profile,
        "accepted_regions",
        "supported_regions",
        "dominant_region",
        "regions",
        "region",
    )
    return values | _track_language_values(taste, "region")


def accepted_audience_profiles(taste: TasteProfile) -> Set[str]:
    profile = taste.source_profile or {}
    values = _profile_values(
        profile,
        "accepted_audience_profiles",
        "audience_profiles",
        "supported_audience_profiles",
    )
    for track in taste.recent_tracks + taste.top_tracks + taste.last_played_tracks:
        audience = normalize_text(track.get("audience_profile"))
        confidence = _number(track.get("audience_confidence"))
        if audience and audience != "unknown" and confidence >= 0.55:
            values.add(audience)
    if not values:
        values.add("general")
    return values


def source_is_trusted(candidate: DiscoveryCandidate) -> bool:
    authority = normalize_text(candidate.item.get("source_authority"))
    return authority in TRUSTED_AUTHORITIES or candidate.item.get("profile_evidence") is True


def _candidate_language(candidate: DiscoveryCandidate) -> tuple[str, float, str]:
    item = candidate.item or {}
    language = normalize_text(item.get("language")) or "unknown"
    confidence = _number(item.get("language_confidence"))
    source = normalize_text(item.get("language_source")) or "unknown"
    if language != "unknown" and confidence <= 0:
        confidence = 0.55
    return language, confidence, source


def _candidate_region(candidate: DiscoveryCandidate) -> tuple[str, float, str]:
    item = candidate.item or {}
    region = normalize_text(item.get("region")) or "unknown"
    confidence = _number(item.get("region_confidence"))
    source = normalize_text(item.get("region_source")) or "unknown"
    if region != "unknown" and confidence <= 0:
        confidence = 0.5
    return region, confidence, source


def _candidate_audience(candidate: DiscoveryCandidate) -> tuple[str, float, str]:
    item = candidate.item or {}
    audience = normalize_text(item.get("audience_profile")) or "general"
    confidence = _number(item.get("audience_confidence"))
    source = normalize_text(item.get("audience_source")) or "unknown"
    if audience != "unknown" and confidence <= 0:
        confidence = 0.55 if audience == "general" else 0.7
    return audience, confidence, source


def _is_direct_history(candidate: DiscoveryCandidate, taste: TasteProfile) -> bool:
    item_id = track_id(candidate.item)
    if not item_id:
        return False
    for track in taste.recent_tracks + taste.top_tracks + taste.last_played_tracks:
        if track_id(track) == item_id:
            return True
    return False


def _evaluate_candidate_profile_compatibility(
    candidate: DiscoveryCandidate,
    taste: TasteProfile,
    *,
    row_kind: str,
    relation_context: str = "",
    taste_match: bool = False,
    strong_personal_match: bool = False,
    negative_suppressed: bool = False,
) -> CompatibilityResult:
    """Single admission decision for home-feed recommendation candidates.

    Search remains exploratory. Home rows must either have a strong personal
    relation or enough profile-compatible metadata/source evidence.
    """

    language, language_confidence, language_source = _candidate_language(candidate)
    region, region_confidence, region_source = _candidate_region(candidate)
    audience_profile, audience_confidence, audience_source = _candidate_audience(candidate)
    profile_languages = accepted_languages(taste)
    profile_regions = accepted_regions(taste)
    profile_audiences = accepted_audience_profiles(taste)
    trusted = source_is_trusted(candidate)
    relation = normalize_text(relation_context)
    strong_relation = bool(
        strong_personal_match
        or _is_direct_history(candidate, taste)
        or candidate.item.get("profile_evidence") is True
        or candidate.item.get("profile_spine") is True
        or relation in {
            "same_artist",
            "same_album",
            "direct_history",
            "artist_neighbor",
            "artist_graph",
            "track_radio",
        }
    )

    def rejected(reason: str, score: float = 0.0) -> CompatibilityResult:
        return CompatibilityResult(
            False,
            score,
            reason,
            language,
            region,
            language_confidence,
            region_confidence,
            language_source,
            region_source,
            audience_profile,
            audience_confidence,
            audience_source,
            reason,
        )

    if row_kind not in DISCOVERY_ROWS:
        return CompatibilityResult(
            True,
            1.0,
            "non_discovery_row",
            language,
            region,
            language_confidence,
            region_confidence,
            language_source,
            region_source,
            audience_profile,
            audience_confidence,
            audience_source,
        )

    if negative_suppressed or normalize_text(candidate.item.get("negative_feedback_state")) in {
        "hidden",
        "removed",
        "hard_suppressed",
    }:
        return rejected("negative_feedback_suppressed")

    sources = {
        normalize_text(source)
        for source in str(candidate.source or "").split("+")
        if normalize_text(source)
    }
    recommendation_path = normalize_text(
        candidate.item.get("recommendation_path")
        or candidate.item.get("relation_type")
    )
    if (
        not taste.is_cold_start
        and row_kind in PERSONAL_PATH_ROWS
        and recommendation_path in BROAD_GLOBAL_PATHS
        and not strong_relation
    ):
        return rejected("missing_personal_recommendation_path")
    if normalize_text(candidate.item.get("source_authority")) == "search_only":
        return CompatibilityResult(
            False,
            0.0,
            "search_only_rejected",
            language,
            region,
            language_confidence,
            region_confidence,
            language_source,
            region_source,
            audience_profile,
            audience_confidence,
            audience_source,
            "search_only_source",
        )

    if strong_relation:
        return CompatibilityResult(
            True,
            1.0,
            relation or "strong_personal_relation",
            language,
            region,
            language_confidence,
            region_confidence,
            language_source,
            region_source,
            audience_profile,
            audience_confidence,
            audience_source,
        )

    if (
        audience_profile not in {"", "unknown", "general"}
        and audience_confidence >= 0.7
        and audience_profile not in profile_audiences
    ):
        return CompatibilityResult(
            False,
            0.06,
            "audience_mismatch",
            language,
            region,
            language_confidence,
            region_confidence,
            language_source,
            region_source,
            audience_profile,
            audience_confidence,
            audience_source,
            "audience_mismatch",
        )

    language_known = language not in {"", "unknown"}
    region_known = region not in {"", "unknown"}
    language_supported = language_known and language in profile_languages
    region_supported = region_known and (
        region in profile_regions or region in GLOBAL_REGION_KEYS
    )
    has_language_profile = bool(profile_languages)
    has_region_profile = bool(profile_regions)
    metadata_confident = (
        (not language_known or language_confidence >= 0.55)
        and (not region_known or region_confidence >= 0.45)
    )

    if has_language_profile and language_known and not language_supported:
        return CompatibilityResult(
            False,
            0.05,
            "language_mismatch",
            language,
            region,
            language_confidence,
            region_confidence,
            language_source,
            region_source,
            audience_profile,
            audience_confidence,
            audience_source,
            "language_mismatch",
        )
    if has_region_profile and region_known and not region_supported:
        return CompatibilityResult(
            False,
            0.1,
            "region_mismatch",
            language,
            region,
            language_confidence,
            region_confidence,
            language_source,
            region_source,
            audience_profile,
            audience_confidence,
            audience_source,
            "region_mismatch",
        )
    if has_language_profile and not language_known and not (trusted and taste_match):
        return CompatibilityResult(
            False,
            0.2,
            "unknown_language_without_bridge",
            language,
            region,
            language_confidence,
            region_confidence,
            language_source,
            region_source,
            audience_profile,
            audience_confidence,
            audience_source,
            "unknown_language_without_bridge",
        )
    if has_region_profile and not region_known and not (trusted and taste_match):
        return CompatibilityResult(
            False,
            0.22,
            "unknown_region_without_bridge",
            language,
            region,
            language_confidence,
            region_confidence,
            language_source,
            region_source,
            audience_profile,
            audience_confidence,
            audience_source,
            "unknown_region_without_bridge",
        )

    if not has_language_profile and language_known:
        # No profile language signal means broad rows need global-safe metadata
        # plus a taste/source bridge. This avoids hardcoded language blocklists.
        if not (
            trusted
            and taste_match
            and metadata_confident
            and (taste.is_cold_start or region in GLOBAL_REGION_KEYS)
        ):
            return CompatibilityResult(
                False,
                0.18,
                "explicit_language_without_profile_bridge",
                language,
                region,
                language_confidence,
                region_confidence,
                language_source,
                region_source,
                audience_profile,
                audience_confidence,
                audience_source,
                "explicit_language_without_profile_bridge",
            )
    if not has_region_profile and region_known and region not in GLOBAL_REGION_KEYS:
        if not (
            trusted
            and taste_match
            and (taste.is_cold_start or language_supported)
        ):
            return CompatibilityResult(
                False,
                0.18,
                "explicit_region_without_profile_bridge",
                language,
                region,
                language_confidence,
                region_confidence,
                language_source,
                region_source,
                audience_profile,
                audience_confidence,
                audience_source,
                "explicit_region_without_profile_bridge",
            )

    if not metadata_confident and not (trusted and taste_match):
        return CompatibilityResult(
            False,
            0.28,
            "low_confidence_metadata",
            language,
            region,
            language_confidence,
            region_confidence,
            language_source,
            region_source,
            audience_profile,
            audience_confidence,
            audience_source,
            "low_confidence_metadata",
        )

    if trusted and taste_match:
        reason = "trusted_profile_bridge"
        score = 0.82
    elif taste_match:
        reason = "profile_bridge"
        score = 0.68
    elif trusted and (language_supported or region_supported):
        reason = "trusted_language_region_match"
        score = 0.62
    else:
        reason = "weak_profile_bridge"
        score = 0.42
    weak_only = bool(sources) and sources.issubset(
        {"ytmusic_home", "popularity", "collaborative", "discovery_universe"}
    )
    exploratory = bool(sources & EXPLORATORY_SOURCES)
    has_profile_signal = bool(
        taste.recent_tracks
        or taste.top_tracks
        or taste.anchor_tracks
        or taste.artist_hints
        or taste.top_artists
        or taste.listened_artists
        or taste.taste_queries
    )
    if has_profile_signal and weak_only and not (strong_relation or (taste_match and trusted)):
        return rejected("weak_broad_source_evidence", 0.2)
    if exploratory and not (strong_relation or taste_match or trusted):
        return rejected("exploratory_source_without_profile_bridge", 0.2)
    return CompatibilityResult(
        True,
        score,
        reason,
        language,
        region,
        language_confidence,
        region_confidence,
        language_source,
        region_source,
        audience_profile,
        audience_confidence,
        audience_source,
    )


class HomeAdmissionPolicy:
    """Canonical home-feed admission authority.

    Candidate acquisition and row builders provide evidence; this policy is
    the only component that decides whether that evidence is sufficient for a
    personalized home surface.
    """

    def evaluate(
        self,
        candidate: DiscoveryCandidate,
        taste: TasteProfile,
        *,
        row_kind: str,
        relation_context: str = "",
        taste_match: bool = False,
        strong_personal_match: bool = False,
        negative_suppressed: bool = False,
    ) -> CompatibilityResult:
        return _evaluate_candidate_profile_compatibility(
            candidate,
            taste,
            row_kind=row_kind,
            relation_context=relation_context,
            taste_match=taste_match,
            strong_personal_match=strong_personal_match,
            negative_suppressed=negative_suppressed,
        )


HOME_ADMISSION_POLICY = HomeAdmissionPolicy()


def candidate_profile_compatibility(
    candidate: DiscoveryCandidate,
    taste: TasteProfile,
    *,
    row_kind: str,
    relation_context: str = "",
    taste_match: bool = False,
    strong_personal_match: bool = False,
    negative_suppressed: bool = False,
) -> CompatibilityResult:
    return HOME_ADMISSION_POLICY.evaluate(
        candidate,
        taste,
        row_kind=row_kind,
        relation_context=relation_context,
        taste_match=taste_match,
        strong_personal_match=strong_personal_match,
        negative_suppressed=negative_suppressed,
    )
