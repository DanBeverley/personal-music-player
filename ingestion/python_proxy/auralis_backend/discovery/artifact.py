from __future__ import annotations

from typing import Any, Dict, List
import json
import time

from ..storage.session_store import get_session_store
from .adapters import row_to_storage_payload
from .config import ARTIFACT_NAMESPACE, ARTIFACT_TTL_SECONDS, ARTIFACT_VERSION, LANE_ORDER, ROW_ORDER
from .schema import DiscoveryArtifact, DiscoveryRow, TasteProfile


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _listened_album_keys(taste: TasteProfile) -> set[str]:
    keys = {_normalized(value) for value in [*taste.album_hints, *taste.top_albums]}
    for track in (
        taste.recent_tracks
        + taste.top_tracks
        + taste.last_played_tracks
        + taste.anchor_tracks
    ):
        album = track.get("album")
        if isinstance(album, dict):
            album = album.get("title") or album.get("name")
        key = _normalized(album)
        if key:
            keys.add(key)
    keys.discard("")
    return keys


def _artifact_key(user_scope_id: str) -> str:
    return f"discovery:home:last_good:{str(user_scope_id or 'guest').strip() or 'guest'}"


def _tiered_artifact_key(user_scope_id: str, quality_tier: str) -> str:
    scope = str(user_scope_id or "guest").strip() or "guest"
    return f"discovery:home:{quality_tier}:{scope}"


def artifact_quality_tier(artifact: DiscoveryArtifact | None) -> str:
    if artifact is None:
        return "rejected"
    return str((artifact.diagnostics or {}).get("artifact_quality") or "").strip().lower() or (
        "launchable" if artifact.accepted else "rejected"
    )


def artifact_quality_score(
    *,
    rows: List[DiscoveryRow],
    quality_tier: str,
    home_tab_diagnostics: Dict[str, Any],
) -> float:
    tier_base = {
        "canonical": 1000.0,
        "launchable": 600.0,
        "partial": 200.0,
        "rejected": 0.0,
    }.get(str(quality_tier or "").strip().lower(), 0.0)
    row_score = min(len(rows or []), 12) * 12.0
    item_score = sum(min(len(row.items or []), 24) for row in rows or []) * 0.35
    lane_counts = dict((home_tab_diagnostics or {}).get("lane_item_counts") or {})
    lane_score = sum(
        min(max(int(count or 0), 0), 24) * 0.2
        for lane_id, count in lane_counts.items()
        if lane_id != "all"
    )
    return round(tier_base + row_score + item_score + lane_score, 3)


def artifact_score(artifact: DiscoveryArtifact | None) -> float:
    if artifact is None:
        return 0.0
    diagnostics = dict(artifact.diagnostics or {})
    stored_score = diagnostics.get("artifact_quality_score")
    if stored_score is None:
        return artifact_quality_score(
            rows=artifact.rows,
            quality_tier=artifact_quality_tier(artifact),
            home_tab_diagnostics=dict(diagnostics.get("home_tab_diagnostics") or {}),
        )
    try:
        return float(stored_score)
    except Exception:
        return artifact_quality_score(
            rows=artifact.rows,
            quality_tier=artifact_quality_tier(artifact),
            home_tab_diagnostics=dict(diagnostics.get("home_tab_diagnostics") or {}),
        )


def _artifact_from_dict(payload: Dict[str, Any] | None) -> DiscoveryArtifact | None:
    if not isinstance(payload, dict):
        return None
    if str(payload.get("artifact_version") or "") != ARTIFACT_VERSION:
        return None
    rows: List[DiscoveryRow] = []
    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        rows.append(
            DiscoveryRow(
                id=str(row.get("id") or row.get("kind") or ""),
                title=str(row.get("title") or ""),
                kind=str(row.get("kind") or row.get("id") or ""),
                item_type=str(row.get("item_type") or "track"),
                row_style=str(row.get("row_style") or ""),
                items=[dict(item) for item in row.get("items") or [] if isinstance(item, dict)],
                meta=dict(row.get("meta") or {}),
                next_offset=int(row.get("next_offset") or 0),
                has_more=bool(row.get("has_more")),
            )
        )
    return DiscoveryArtifact(
        session_id=str(payload.get("session_id") or ""),
        user_scope_id=str(payload.get("user_scope_id") or "guest"),
        profile_key=str(payload.get("profile_key") or ""),
        generated_at=float(payload.get("generated_at") or 0.0),
        expires_at=float(payload.get("expires_at") or 0.0),
        rows=rows,
        diagnostics=dict(payload.get("diagnostics") or {}),
        candidate_pool_counts=dict(payload.get("candidate_pool_counts") or {}),
        provider_timings_ms=dict(payload.get("provider_timings_ms") or {}),
        home_tab_lanes=dict(payload.get("home_tab_lanes") or {}),
        accepted=bool(payload.get("accepted")),
        quality_reasons=list(payload.get("quality_reasons") or []),
        artifact_source=str(payload.get("artifact_source") or "cache"),
    )


def artifact_to_dict(artifact: DiscoveryArtifact) -> Dict[str, Any]:
    return {
        "artifact_version": ARTIFACT_VERSION,
        "session_id": artifact.session_id,
        "user_scope_id": artifact.user_scope_id,
        "profile_key": artifact.profile_key,
        "generated_at": artifact.generated_at,
        "expires_at": artifact.expires_at,
        "rows": [row_to_storage_payload(row) for row in artifact.rows],
        "diagnostics": dict(artifact.diagnostics or {}),
        "candidate_pool_counts": dict(artifact.candidate_pool_counts or {}),
        "provider_timings_ms": dict(artifact.provider_timings_ms or {}),
        "home_tab_lanes": dict(artifact.home_tab_lanes or {}),
        "accepted": bool(artifact.accepted),
        "quality_reasons": list(artifact.quality_reasons or []),
        "artifact_source": artifact.artifact_source,
    }


def _persistent_get(server: Any, key: str) -> Dict[str, Any] | None:
    from ..recommend.store_runtime import open_recommendation_store_connection

    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return None
    try:
        row = connection.execute(
            """
            SELECT payload_json
            FROM recommendation_feature_store
            WHERE namespace = ? AND entity_id = ?
            """,
            [ARTIFACT_NAMESPACE, key],
        ).fetchone()
    except Exception:
        return None
    finally:
        connection.close()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        return None
    return dict(payload) if isinstance(payload, dict) else None


def _persistent_set(server: Any, key: str, payload: Dict[str, Any]) -> None:
    from ..recommend.store_runtime import open_recommendation_store_connection

    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return
    try:
        connection.execute(
            """
            INSERT INTO recommendation_feature_store(namespace, entity_id, model_id, payload_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(namespace, entity_id) DO UPDATE SET
                model_id = excluded.model_id,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            [
                ARTIFACT_NAMESPACE,
                key,
                ARTIFACT_VERSION,
                json.dumps(payload, ensure_ascii=False),
                time.time(),
            ],
        )
        connection.commit()
    except Exception:
        return
    finally:
        connection.close()


def load_cached_artifact(server: Any, user_scope_id: str) -> DiscoveryArtifact | None:
    artifacts: List[DiscoveryArtifact] = []
    for key in (
        _tiered_artifact_key(user_scope_id, "canonical"),
        _tiered_artifact_key(user_scope_id, "launchable"),
        _artifact_key(user_scope_id),
    ):
        payload = None
        try:
            payload = get_session_store().get(key)
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            payload = _persistent_get(server, key)
        artifact = _artifact_from_dict(payload)
        if artifact is None or not artifact.accepted:
            continue
        if artifact.expires_at and artifact.expires_at <= time.time():
            continue
        artifact.artifact_source = "cache"
        artifacts.append(artifact)
    if not artifacts:
        return None
    return max(artifacts, key=artifact_score)


def store_accepted_artifact(server: Any, artifact: DiscoveryArtifact) -> None:
    if not artifact.accepted:
        return
    quality_tier = artifact_quality_tier(artifact)
    if quality_tier not in {"canonical", "launchable"}:
        return
    key = _tiered_artifact_key(artifact.user_scope_id, quality_tier)
    payload = artifact_to_dict(artifact)
    try:
        get_session_store().set(key, payload, ARTIFACT_TTL_SECONDS)
    except Exception:
        pass
    _persistent_set(server, key, payload)


def invalidate_cached_artifact(server: Any, user_scope_id: str) -> None:
    keys = [
        _artifact_key(user_scope_id),
        _tiered_artifact_key(user_scope_id, "canonical"),
        _tiered_artifact_key(user_scope_id, "launchable"),
    ]
    for key in keys:
        try:
            get_session_store().delete(key)
        except Exception:
            pass

    from ..recommend.store_runtime import open_recommendation_store_connection

    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return
    try:
        connection.execute(
            """
            DELETE FROM recommendation_feature_store
            WHERE namespace = ? AND entity_id = ?
            """,
            [ARTIFACT_NAMESPACE, keys[0]],
        )
        for key in keys[1:]:
            connection.execute(
                """
                DELETE FROM recommendation_feature_store
                WHERE namespace = ? AND entity_id = ?
                """,
                [ARTIFACT_NAMESPACE, key],
            )
        connection.commit()
    except Exception:
        pass
    finally:
        connection.close()


def evaluate_quality(
    *,
    rows: List[DiscoveryRow],
    taste: TasteProfile,
    home_tab_diagnostics: Dict[str, Any],
) -> tuple[bool, List[str], str]:
    if taste.is_cold_start:
        return False, ["cold_start_not_cached_as_personalized"], "rejected"
    row_ids = [row.kind for row in rows or []]
    row_counts = {row.kind: len(row.items or []) for row in rows or []}
    reasons: List[str] = []
    if len(row_ids) < 7:
        reasons.append("below_min_accepted_rows")
    if "made_for_you" not in row_ids:
        reasons.append("missing_made_for_you")
    if "featured_new_albums" not in row_ids and "recommended_albums" not in row_ids:
        reasons.append("missing_album_feature")
    if "trending_by_genre" in row_ids:
        genre_row = next((row for row in rows if row.kind == "trending_by_genre"), None)
        tabs = ((genre_row.meta or {}).get("tabs") if genre_row is not None else []) or []
        if not isinstance(tabs, list) or len(tabs) < 2:
            reasons.append("trending_by_genre_below_min_tabs")
    else:
        reasons.append("missing_trending_by_genre")
    if "recommended_artists" not in row_ids and "quiet_picks" not in row_ids:
        reasons.append("missing_artist_or_quiet_picks")
    recommended_albums = next(
        (row for row in rows if row.kind == "recommended_albums"),
        None,
    )
    if recommended_albums is not None and recommended_albums.items:
        listened_albums = _listened_album_keys(taste)
        album_titles = {
            _normalized(item.get("title") or item.get("album"))
            for item in recommended_albums.items
            if isinstance(item, dict)
        }
        album_titles.discard("")
        if album_titles and album_titles.issubset(listened_albums):
            reasons.append("recommended_albums_listened_only")
    if row_ids == ["last_played", "frequently_listened"] or len(row_ids) <= 2:
        reasons.append("weak_two_row_feed")
    lane_counts = dict((home_tab_diagnostics or {}).get("lane_item_counts") or {})
    good_non_all_lanes = []
    for lane_id, count in lane_counts.items():
        try:
            item_count = int(count or 0)
        except Exception:
            item_count = 0
        if lane_id != "all" and item_count >= 6:
            good_non_all_lanes.append(lane_id)
    has_usable_partial_lanes = len(good_non_all_lanes) >= 2
    if not bool((home_tab_diagnostics or {}).get("accepted")) and not has_usable_partial_lanes:
        reasons.append("home_tabs_not_accepted")
    tab_rejections = [
        str(reason or "")
        for reason in (home_tab_diagnostics or {}).get("rejection_reasons") or []
    ]
    if any(reason.endswith(":too_similar") for reason in tab_rejections):
        reasons.append("home_tabs_too_similar")
    lanes = list(lane_counts)
    if lanes == ["all"] or set(lanes) == {"all"} or (lanes and not good_non_all_lanes):
        reasons.append("single_all_tab_only")
    for row_kind in ROW_ORDER:
        if row_kind in row_ids and row_counts.get(row_kind, 0) <= 0:
            reasons.append(f"{row_kind}:empty")
    canonical_only_reasons = {
        "missing_made_for_you",
        "missing_trending_by_genre",
    }
    hard_reasons = [
        reason
        for reason in reasons
        if reason not in canonical_only_reasons
    ]
    if not reasons:
        return True, [], "canonical"
    if not hard_reasons:
        return True, reasons, "launchable"
    if len(row_ids) >= 4 and not any(
        reason in {"weak_two_row_feed", "missing_album_feature", "single_all_tab_only"}
        for reason in hard_reasons
    ):
        return False, reasons, "partial"
    return False, reasons, "rejected"


def build_diagnostics(
    *,
    artifact_source: str,
    artifact_quality: str,
    row_status: Dict[str, Any],
    rows: List[DiscoveryRow],
    candidate_pool_counts: Dict[str, int],
    provider_timings_ms: Dict[str, int],
    home_tab_lanes: Dict[str, Dict[str, Any]],
    home_tab_diagnostics: Dict[str, Any],
    quality_reasons: List[str],
    elapsed_ms: int,
) -> Dict[str, Any]:
    breadth = {
        key.removeprefix("breadth_"): value
        for key, value in candidate_pool_counts.items()
        if key.startswith("breadth_")
    }
    return {
        "engine": "discovery_engine",
        "artifact_source": artifact_source,
        "artifact_quality": artifact_quality,
        "row_status": row_status,
        "row_item_counts": {row.kind: len(row.items or []) for row in rows or []},
        "row_order": [row.kind for row in rows or []],
        "home_tab_lanes": home_tab_lanes,
        "home_tab_diagnostics": home_tab_diagnostics,
        "candidate_pool_counts": candidate_pool_counts,
        "candidate_breadth": breadth,
        "provider_timings_ms": provider_timings_ms,
        "row_reserve_counts": {
            row.kind: int((row.meta or {}).get("reserve_count") or 0)
            for row in rows or []
        },
        "pageable_rows": [row.kind for row in rows or [] if row.has_more],
        "quality_reasons": quality_reasons,
        "artifact_quality_score": artifact_quality_score(
            rows=rows,
            quality_tier=artifact_quality,
            home_tab_diagnostics=home_tab_diagnostics,
        ),
        "deferred_row_kinds": [],
        "deferred_rows_pending": False,
        "launch_tier_only": artifact_quality != "canonical",
        "time_to_first_home_payload_ms": elapsed_ms,
        "total_build_ms": elapsed_ms,
    }
