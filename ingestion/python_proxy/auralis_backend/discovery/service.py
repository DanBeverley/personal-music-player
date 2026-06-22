from __future__ import annotations

from typing import Any, Dict
import hashlib
import json
import threading
import time
import uuid

from fastapi import HTTPException

from ..recommend.session_runtime import _store_feed_session, load_feed_session
from ..search.catalog_pipeline import schedule_catalog_population
from .adapters import artifact_to_session, home_response_from_artifact, row_page_response_from_artifact
from .artifact import (
    ARTIFACT_TTL_SECONDS,
    artifact_quality_tier,
    artifact_score,
    build_diagnostics,
    evaluate_quality,
    load_cached_artifact,
    store_accepted_artifact,
)
from .candidates import build_candidate_pools
from .config import ARTIFACT_VERSION, ENGINE_MODEL_VERSION
from .ranking import build_rows_from_pools
from .schema import DiscoveryArtifact
from .signals import build_taste_profile


class DiscoveryService:
    def __init__(self, server: Any) -> None:
        self._server = server
        self._last_background_builds: Dict[str, float] = {}
        self._background_builds_inflight: set[str] = set()
        self._background_build_lock = threading.Lock()

    def _background_fingerprint(self, taste: Any) -> str:
        return f"{taste.user_scope_id}:{taste.profile_key}"

    def _artifact_signature(self, artifact: DiscoveryArtifact | None) -> str:
        if artifact is None:
            return ""
        payload = [
            [
                row.kind,
                [
                    str(item.get("id") or item.get("videoId") or item.get("title") or "")
                    for item in row.items or []
                ],
            ]
            for row in artifact.rows or []
        ]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()

    def _refresh_overlap(self, previous: DiscoveryArtifact | None, current: DiscoveryArtifact) -> float:
        def ids(artifact: DiscoveryArtifact | None) -> set[str]:
            return {
                str(item.get("id") or item.get("videoId") or "").strip()
                for row in (artifact.rows if artifact is not None else [])
                for item in row.items or []
                if str(item.get("id") or item.get("videoId") or "").strip()
            }

        previous_ids = ids(previous)
        current_ids = ids(current)
        if not previous_ids or not current_ids:
            return 0.0
        return len(previous_ids & current_ids) / max(len(previous_ids | current_ids), 1)

    def _changed_row_kinds(
        self,
        previous: DiscoveryArtifact | None,
        current: DiscoveryArtifact,
    ) -> list[str]:
        def row_signatures(artifact: DiscoveryArtifact | None) -> Dict[str, tuple[str, ...]]:
            if artifact is None:
                return {}
            return {
                row.kind: tuple(
                    str(
                        item.get("id")
                        or item.get("videoId")
                        or item.get("canonical_source_identity")
                        or item.get("title")
                        or ""
                    ).strip()
                    for item in row.items or []
                    if str(
                        item.get("id")
                        or item.get("videoId")
                        or item.get("canonical_source_identity")
                        or item.get("title")
                        or ""
                    ).strip()
                )
                for row in artifact.rows or []
            }

        previous_rows = row_signatures(previous)
        current_rows = row_signatures(current)
        return [
            kind
            for kind, signature in current_rows.items()
            if previous_rows.get(kind) != signature
        ]

    def _tier_rank(self, tier: str) -> int:
        return {
            "rejected": 0,
            "partial": 1,
            "launchable": 2,
            "canonical": 3,
        }.get(str(tier or "").strip().lower(), 0)

    def _explicit_refresh_comparable(
        self,
        *,
        changed_row_kinds: list[str],
        previous_tier: str,
        new_tier: str,
        previous_score: float,
        new_score: float,
    ) -> bool:
        if not changed_row_kinds:
            return False
        # Pull-to-refresh is an explicit user action: if the new artifact is in
        # the same quality family and rotates visible rows, allow a modest score
        # dip instead of making the first pull feel inert.
        if self._tier_rank(new_tier) + 1 < self._tier_rank(previous_tier):
            return False
        important_rows = {
            "todays_pick",
            "featured_new_albums",
            "made_for_you",
            "because_you_played",
            "trending_by_genre",
            "recommended_albums",
            "recommended_artists",
            "quiet_picks",
            "hidden_gems",
        }
        important_change_count = len(set(changed_row_kinds) & important_rows)
        if important_change_count >= 3 and self._tier_rank(new_tier) >= max(
            1,
            self._tier_rank(previous_tier) - 1,
        ):
            return new_score >= max(previous_score - 320.0, previous_score * 0.72)
        return new_score >= max(previous_score - 220.0, previous_score * 0.8)

    def _claim_background_build(self, fingerprint: str) -> bool:
        with self._background_build_lock:
            last_build = self._last_background_builds.get(fingerprint, 0.0)
            if (
                fingerprint in self._background_builds_inflight
                or time.time() - last_build < 45.0
            ):
                return False
            self._background_builds_inflight.add(fingerprint)
            return True

    def _release_background_build(self, fingerprint: str) -> None:
        with self._background_build_lock:
            self._background_builds_inflight.discard(fingerprint)
            self._last_background_builds[fingerprint] = time.time()

    def recommend(
        self,
        req: Any,
        *,
        request_mode: str,
        trace: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        request_id = str((trace or {}).get("request_id") or uuid.uuid4())
        page_size = max(int(getattr(self._server, "RECOMMENDATION_ROW_PAGE_SIZE", 8) or 8), 1)
        if request_mode == "row_page":
            return self._row_page(req, request_id=request_id)

        force_refresh = bool(getattr(req, "force_refresh", False) or getattr(req, "prefer_fresh_rows", False))
        taste = build_taste_profile(self._server, req)
        schedule_catalog_population(
            self._server,
            user_scope_id=taste.user_scope_id,
            req=req,
            taste=taste,
            reason=f"discovery_{request_mode}",
            run_musicbrainz=request_mode == "background_prepare",
            min_interval_seconds=45.0 if request_mode == "background_prepare" else 120.0,
        )
        previous = load_cached_artifact(self._server, taste.user_scope_id)
        if request_mode == "full_feed" and not force_refresh:
            if previous is not None:
                self._store_session(previous)
                return home_response_from_artifact(
                    previous,
                    request_id=request_id,
                    page_size=page_size,
                )
        background_fingerprint = self._background_fingerprint(taste)
        background_claimed = False
        if (
            request_mode == "background_prepare"
            and not force_refresh
            and previous is not None
        ):
            background_claimed = self._claim_background_build(background_fingerprint)
            if not background_claimed:
                previous.diagnostics = dict(previous.diagnostics or {})
                previous.diagnostics.update(
                    {
                        "artifact_quality": "kept_previous",
                        "refresh_outcome": "suppressed_same_fingerprint",
                        "refresh_changed": False,
                        "refresh_fingerprint": background_fingerprint,
                    }
                )
                self._store_session(previous)
                return home_response_from_artifact(
                    previous,
                    request_id=request_id,
                    page_size=page_size,
                )

        try:
            artifact = self._build_artifact(
                req,
                taste=taste,
                artifact_source="background_prepare" if request_mode == "background_prepare" else "fresh_build",
                request_id=request_id,
            )
        finally:
            if background_claimed:
                self._release_background_build(background_fingerprint)
        if artifact.accepted:
            overlap = self._refresh_overlap(previous, artifact)
            changed = self._artifact_signature(previous) != self._artifact_signature(artifact)
            changed_row_kinds = self._changed_row_kinds(previous, artifact)
            previous_score = artifact_score(previous)
            new_score = artifact_score(artifact)
            artifact.diagnostics = dict(artifact.diagnostics or {})
            artifact.diagnostics.update(
                {
                    "refresh_outcome": "changed" if changed else "unchanged",
                    "refresh_changed": changed,
                    "refresh_overlap": round(overlap, 4),
                    "changed_row_kinds": changed_row_kinds,
                    "todays_pick_changed": "todays_pick" in changed_row_kinds,
                    "featured_albums_changed": "featured_new_albums" in changed_row_kinds,
                    "refresh_fingerprint": background_fingerprint,
                    "previous_artifact_quality_score": previous_score,
                    "new_artifact_quality_score": new_score,
                }
            )
            if previous is not None and not changed:
                previous.diagnostics = dict(previous.diagnostics or {})
                previous.diagnostics.update(artifact.diagnostics)
                previous.diagnostics["artifact_quality"] = "kept_previous"
                previous.diagnostics["refresh_outcome"] = "unchanged"
                previous.diagnostics["refresh_changed"] = False
                self._store_session(previous)
                return home_response_from_artifact(
                    previous,
                    request_id=request_id,
                    page_size=page_size,
                )
            previous_tier = artifact_quality_tier(previous)
            new_tier = artifact_quality_tier(artifact)
            user_requested_refresh = force_refresh and changed
            comparable_refresh = user_requested_refresh and self._explicit_refresh_comparable(
                changed_row_kinds=changed_row_kinds,
                previous_tier=previous_tier,
                new_tier=new_tier,
                previous_score=previous_score,
                new_score=new_score,
            )
            if previous is not None and new_score < previous_score and not comparable_refresh:
                previous.diagnostics = dict(previous.diagnostics or {})
                previous.diagnostics.update(
                    {
                        "artifact_quality": "kept_previous",
                        "refresh_outcome": "kept_previous",
                        "refresh_changed": False,
                        "refresh_fingerprint": background_fingerprint,
                        "previous_artifact_quality_tier": previous_tier,
                        "rejected_artifact_quality_tier": new_tier,
                        "previous_artifact_quality_score": previous_score,
                        "new_artifact_quality_score": new_score,
                        "rejected_refresh_reasons": [
                            "quality_regression",
                            *list(artifact.quality_reasons or []),
                        ],
                    }
                )
                self._store_session(previous)
                return home_response_from_artifact(
                    previous,
                    request_id=request_id,
                    page_size=page_size,
                )
            if comparable_refresh:
                artifact.diagnostics["refresh_outcome"] = "changed_user_requested"
                artifact.diagnostics["refresh_changed"] = True
                artifact.diagnostics["accepted_refresh_despite_lower_score"] = True
                artifact.diagnostics["explicit_refresh_comparable"] = True
            store_accepted_artifact(self._server, artifact)
            self._store_session(artifact)
            return home_response_from_artifact(
                artifact,
                request_id=request_id,
                page_size=page_size,
            )

        if previous is not None:
            previous.diagnostics = dict(previous.diagnostics or {})
            previous.diagnostics["artifact_quality"] = "kept_previous"
            previous.diagnostics["rejected_refresh_reasons"] = list(artifact.quality_reasons or [])
            previous.diagnostics["refresh_outcome"] = "kept_previous"
            previous.diagnostics["refresh_changed"] = False
            previous.diagnostics["refresh_fingerprint"] = background_fingerprint
            self._store_session(previous)
            return home_response_from_artifact(
                previous,
                request_id=request_id,
                page_size=page_size,
            )

        self._store_session(artifact)
        return home_response_from_artifact(
            artifact,
            request_id=request_id,
            page_size=page_size,
        )

    def _row_page(self, req: Any, *, request_id: str) -> Dict[str, Any]:
        row_id = self._trim(getattr(req, "row_id", ""))
        session_id = self._trim(getattr(req, "session_id", ""))
        offset = max(int(getattr(req, "offset", 0) or 0), 0)
        limit = max(int(getattr(req, "limit", 8) or 8), 1)
        session = load_feed_session(self._server, session_id)
        if isinstance(session, dict) and row_id:
            artifact = self._artifact_from_session(session)
            if artifact is not None:
                response = row_page_response_from_artifact(
                    artifact,
                    row_id=row_id,
                    offset=offset,
                    limit=limit,
                    request_id=request_id,
                )
                if response is not None:
                    return response
        cached = load_cached_artifact(self._server, self._trim(getattr(req, "user_scope_id", "")) or "guest")
        if cached is not None and row_id:
            response = row_page_response_from_artifact(
                cached,
                row_id=row_id,
                offset=offset,
                limit=limit,
                request_id=request_id,
            )
            if response is not None:
                return response
        raise HTTPException(status_code=404, detail="Discovery row is not available")

    def _build_artifact(
        self,
        req: Any,
        *,
        taste: Any | None = None,
        artifact_source: str,
        request_id: str,
    ) -> DiscoveryArtifact:
        started = time.perf_counter()
        taste = taste or build_taste_profile(self._server, req)
        now = time.time()
        if taste.is_cold_start or bool(getattr(req, "fresh_account_empty_home", False)):
            diagnostics = build_diagnostics(
                artifact_source=artifact_source,
                artifact_quality="rejected",
                row_status={},
                rows=[],
                candidate_pool_counts={},
                provider_timings_ms={},
                home_tab_lanes={},
                home_tab_diagnostics={"accepted": False, "rejection_reasons": ["cold_start"]},
                quality_reasons=["cold_start_not_cached_as_personalized"],
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
            diagnostics["fresh_account_empty_home"] = True
            diagnostics["client_signal_tier"] = "cold_start"
            return DiscoveryArtifact(
                session_id=str(uuid.uuid4()),
                user_scope_id=taste.user_scope_id,
                profile_key=taste.profile_key,
                generated_at=now,
                expires_at=now + ARTIFACT_TTL_SECONDS,
                rows=[],
                diagnostics=diagnostics,
                candidate_pool_counts={},
                provider_timings_ms={},
                home_tab_lanes={},
                accepted=False,
                quality_reasons=["cold_start_not_cached_as_personalized"],
                artifact_source=artifact_source,
            )

        pools, candidate_counts, provider_timings = build_candidate_pools(self._server, taste)
        rows, row_status, home_tab_lanes, home_tab_diagnostics = build_rows_from_pools(pools, taste)
        accepted, quality_reasons, artifact_quality = evaluate_quality(
            rows=rows,
            taste=taste,
            home_tab_diagnostics=home_tab_diagnostics,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        diagnostics = build_diagnostics(
            artifact_source=artifact_source,
            artifact_quality=artifact_quality,
            row_status=row_status,
            rows=rows,
            candidate_pool_counts=candidate_counts,
            provider_timings_ms=provider_timings,
            home_tab_lanes=home_tab_lanes,
            home_tab_diagnostics=home_tab_diagnostics,
            quality_reasons=quality_reasons,
            elapsed_ms=elapsed_ms,
        )
        diagnostics["request_id"] = request_id
        diagnostics["model_version"] = ENGINE_MODEL_VERSION
        diagnostics["client_signal_tier"] = taste.signal_tier
        diagnostics["profile_key"] = taste.profile_key
        diagnostics["refresh_requested"] = bool(taste.force_refresh)
        diagnostics["avoid_ids_count"] = len(taste.avoid_ids or [])
        diagnostics["refresh_token_present"] = bool(str(taste.refresh_token or "").strip())
        return DiscoveryArtifact(
            session_id=str(uuid.uuid4()),
            user_scope_id=taste.user_scope_id,
            profile_key=taste.profile_key,
            generated_at=now,
            expires_at=now + ARTIFACT_TTL_SECONDS,
            rows=rows,
            diagnostics=diagnostics,
            candidate_pool_counts=candidate_counts,
            provider_timings_ms=provider_timings,
            home_tab_lanes=home_tab_lanes,
            accepted=accepted,
            quality_reasons=quality_reasons,
            artifact_source=artifact_source,
        )

    def _store_session(self, artifact: DiscoveryArtifact) -> None:
        try:
            session = artifact_to_session(artifact)
            _store_feed_session(self._server, session)
            visible_rows = []
            page_size = max(
                int(getattr(self._server, "RECOMMENDATION_ROW_PAGE_SIZE", 8) or 8),
                1,
            )
            for row in session.get("rows") or []:
                if not isinstance(row, dict):
                    continue
                visible = dict(row)
                visible["items"] = list(row.get("items") or [])[:page_size]
                visible_rows.append(visible)
            self._server._recommendation_record_impressions(session, visible_rows)
        except Exception:
            return

    def _artifact_from_session(self, session: Dict[str, Any]) -> DiscoveryArtifact | None:
        rows = []
        from .artifact import _artifact_from_dict

        payload = {
            "artifact_version": ARTIFACT_VERSION,
            "session_id": session.get("session_id"),
            "user_scope_id": session.get("user_scope_id") or "guest",
            "profile_key": session.get("profile_key") or "",
            "generated_at": session.get("generated_at") or time.time(),
            "expires_at": session.get("expires_at") or time.time() + ARTIFACT_TTL_SECONDS,
            "rows": session.get("rows") or rows,
            "diagnostics": session.get("diagnostics") or {},
            "candidate_pool_counts": (session.get("diagnostics") or {}).get("candidate_pool_counts") or {},
            "provider_timings_ms": (session.get("diagnostics") or {}).get("provider_timings_ms") or {},
            "home_tab_lanes": (session.get("diagnostics") or {}).get("home_tab_lanes") or {},
            "accepted": True,
            "quality_reasons": (session.get("diagnostics") or {}).get("quality_reasons") or [],
            "artifact_source": "cache",
        }
        return _artifact_from_dict(payload)

    def _trim(self, value: Any) -> str:
        try:
            return self._server._recommendation_trim_text(value)
        except Exception:
            return str(value or "").strip()
