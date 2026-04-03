from __future__ import annotations

import time
from typing import Any
from typing import Dict, List

from ..domain.catalog import cache_search_payload
from ..domain.features import build_search_profile
from ..domain.retrieval import retrieve_search_candidates_fast
from ..legacy import get_server
from ..recommend.precompute import (
    build_search_snapshot,
    get_search_snapshot,
    get_search_snapshot_for_profile,
    runtime_snapshot as precompute_runtime_snapshot,
    schedule_search_warmup,
)
from ..recommend.feature_store import request_store_runtime
from .pipeline import (
    build_search_ranking_runtime,
    rank_album_candidates,
    rank_artist_candidates,
    rank_track_candidates,
    rank_track_candidates_fast_path,
    summarize_ranked_results,
)
from .runtime import search_query_intent, semantic_search_suggestions


class SearchService:
    def __init__(self, server: Any | None = None) -> None:
        self._server = server or get_server()

    def _rank_track_candidates(
        self,
        req,
        profile: Dict[str, Any],
        retrieval_payload: Dict[str, Any],
        *,
        limit: int,
        ranking_runtime: Dict[str, Any] | None = None,
    ) -> List[Dict[str, Any]]:
        return rank_track_candidates(
            self._server,
            req,
            profile,
            retrieval_payload,
            limit=limit,
            ranking_runtime=ranking_runtime,
        )

    def _rank_track_candidates_fast_path(
        self,
        req,
        retrieval_payload: Dict[str, Any],
        *,
        limit: int,
    ) -> List[Dict[str, Any]]:
        return rank_track_candidates_fast_path(
            self._server,
            req,
            retrieval_payload,
            limit=limit,
        )

    def _rank_artist_candidates(
        self,
        req,
        profile: Dict[str, Any],
        retrieval_payload: Dict[str, Any],
        *,
        limit: int,
        ranking_runtime: Dict[str, Any] | None = None,
    ) -> List[Dict[str, Any]]:
        return rank_artist_candidates(
            self._server,
            req,
            profile,
            retrieval_payload,
            limit=limit,
            ranking_runtime=ranking_runtime,
        )

    def _rank_album_candidates(
        self,
        req,
        profile: Dict[str, Any],
        retrieval_payload: Dict[str, Any],
        *,
        limit: int,
        ranking_runtime: Dict[str, Any] | None = None,
    ) -> List[Dict[str, Any]]:
        return rank_album_candidates(
            self._server,
            req,
            profile,
            retrieval_payload,
            limit=limit,
            ranking_runtime=ranking_runtime,
        )

    def _materialize_side_candidates(
        self,
        retrieval_payload: Dict[str, Any],
        *,
        candidate_key: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        server = self._server
        candidates = retrieval_payload.get(candidate_key) or {}
        if not candidates or limit <= 0:
            return []
        results: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for entry in candidates.values():
            payload = dict((entry or {}).get("payload") or {})
            if not payload:
                continue
            if candidate_key == "artist_candidates":
                dedupe_key = server._normalize_text(payload.get("name") or "") or server._recommendation_trim_text(payload.get("id"))
            else:
                dedupe_key = (
                    server._recommendation_trim_text(payload.get("id"))
                    or f"{server._normalize_text(payload.get('title') or '')}|{server._normalize_text(payload.get('artist') or '')}"
                )
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            source_scores = dict((entry or {}).get("source_scores") or {})
            source_names = sorted(
                source_name
                for source_name in source_scores.keys()
                if server._recommendation_trim_text(source_name)
            )
            if source_names:
                payload["search_source"] = source_names[0]
                payload["search_sources"] = source_names
            results.append(payload)
            if len(results) >= limit:
                break
        return results

    def search(self, req):
        server = self._server
        trace = server._trace_start(
            "search",
            user_scope_id=req.user_scope_id or "guest",
            surface=req.surface or "home_feed",
            query=req.query or "",
        )
        request_started_at = time.perf_counter()
        query = server._recommendation_trim_text(req.query)
        limit = max(8, min(req.limit or 16, 16))
        track_model_version = server._ranking_model_version("search_track_reranker_v2")
        try:
            with request_store_runtime(allow_persistent_reads=False):
                print(
                    "[EBB:search][progress] "
                    f"request_id={trace.get('request_id') or ''} stage=request_parse query={query[:48]}",
                    flush=True,
                )
                if not query:
                    response = {
                        "status": "success",
                        "request_id": trace["request_id"],
                        "model_version": track_model_version,
                        "query_intent": "mixed",
                        "results": [],
                        "tracks": [],
                        "artists": [],
                        "albums": [],
                        "similar_artists": [],
                        "diagnostics": {
                            "ranking_backend": "search_service_v41",
                            "empty_query": True,
                        },
                    }
                    response["diagnostics"].update(
                        server._trace_diagnostics(server._trace_finalize(trace, status="success"))
                    )
                    server._trace_log_request(
                        trace,
                        request_type="search",
                        user_scope_id=req.user_scope_id or "guest",
                        model_version=track_model_version,
                    )
                    return response

                parse_started_at = time.perf_counter()
                url_match = server.re.search(
                    r"(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})",
                    query,
                )
                server._trace_stage(trace, "search.request_parse", parse_started_at)
                if url_match:
                    video_id = url_match.group(1)
                    watch = server._upstream_call_with_retry(
                        lambda: server.ytmusic.get_watch_playlist(videoId=video_id),
                        attempts=server.UPSTREAM_RETRY_ATTEMPTS,
                        backoff_seconds=server.UPSTREAM_RETRY_BACKOFF_SECONDS,
                        default={},
                    )
                    vd = (watch or {}).get("videoDetails", {})
                    track_payload = server.normalize_recommendation_track(
                        {
                            "id": video_id,
                            "title": vd.get("title") or "Unknown URL Track",
                            "duration": vd.get("lengthSeconds") or 0,
                            "thumbnail": server.extract_thumbnail(vd),
                            "channel": server.extract_artist(vd),
                        }
                    )
                    tracks = [track_payload] if track_payload is not None else []
                    response = {
                        "status": "success",
                        "request_id": trace["request_id"],
                        "model_version": track_model_version,
                        "query_intent": "track",
                        "top_result": {
                            "entity_type": "track",
                            "item": tracks[0],
                        } if tracks else None,
                        "results": tracks,
                        "tracks": tracks,
                        "artists": [],
                        "albums": [],
                        "similar_artists": [],
                        "diagnostics": {
                            "ranking_backend": "search_service_v41",
                            "url_query": True,
                        },
                    }
                    response["diagnostics"].update(
                        server._trace_diagnostics(server._trace_finalize(trace, status="success"))
                    )
                    server._trace_log_request(
                        trace,
                        request_type="search",
                        user_scope_id=req.user_scope_id or "guest",
                        model_version=track_model_version,
                    )
                    return response

                profile_started_at = time.perf_counter()
                legacy_req, profile = build_search_profile(req)
                server._trace_stage(trace, "search.profile_build", profile_started_at)
                print(
                    "[EBB:search][progress] "
                    f"request_id={trace.get('request_id') or ''} "
                    f"stage=profile_build done profile_ms={int((time.perf_counter() - profile_started_at) * 1000)}",
                    flush=True,
                )

                retrieval_started_at = time.perf_counter()
                precompute_snapshot = None
                precompute_hit = False
                if not bool(req.force_refresh):
                    precompute_snapshot = get_search_snapshot(
                        user_scope_id=req.user_scope_id or "guest",
                        query=query,
                    )
                    if isinstance(precompute_snapshot, dict):
                        retrieval_payload = dict(
                            (precompute_snapshot.get("retrieval_payload") or {})
                        )
                        precompute_hit = bool(retrieval_payload)
                    else:
                        retrieval_payload = {}
                else:
                    retrieval_payload = {}
                if not precompute_hit and not bool(req.force_refresh):
                    precompute_snapshot = get_search_snapshot_for_profile(
                        profile_key=profile.get("profile_key") or "",
                        query=query,
                    )
                    if isinstance(precompute_snapshot, dict):
                        retrieval_payload = dict(
                            (precompute_snapshot.get("retrieval_payload") or {})
                        )
                        precompute_hit = bool(retrieval_payload)
                precompute_stale = bool((precompute_snapshot or {}).get("stale"))
                if not precompute_hit:
                    if bool(req.force_refresh):
                        try:
                            precompute_snapshot = build_search_snapshot(
                                server=server,
                                user_scope_id=req.user_scope_id or "guest",
                                query=query,
                                force=True,
                                legacy_req=legacy_req,
                                profile=profile,
                            )
                            retrieval_payload = dict(
                                (precompute_snapshot or {}).get("retrieval_payload") or {}
                            )
                            precompute_hit = bool(retrieval_payload)
                        except Exception:
                            retrieval_payload = {}
                    if not precompute_hit:
                        retrieval_payload = retrieve_search_candidates_fast(
                            legacy_req,
                            profile,
                            limit=limit,
                        )
                elif precompute_stale:
                    schedule_search_warmup(
                        user_scope_id=req.user_scope_id or "guest",
                        query=query,
                    )
                print(
                    "[EBB:search][progress] "
                    f"request_id={trace.get('request_id') or ''} "
                    f"stage=retrieval resolved_hit={precompute_hit} stale={precompute_stale} "
                    f"source={((precompute_snapshot or {}).get('resolved_from') or '')}",
                    flush=True,
                )
            server._trace_stage(trace, "search.retrieval", retrieval_started_at)
            query_intent = retrieval_payload.get("query_intent") or search_query_intent(query)
            ranked_snapshot = dict((precompute_snapshot or {}).get("ranked_results") or {})
            precomputed_tracks = list(ranked_snapshot.get("tracks") or [])
            precomputed_artists = list(ranked_snapshot.get("artists") or [])
            precomputed_albums = list(ranked_snapshot.get("albums") or [])
            precomputed_ranked_hit = bool(
                precompute_hit and (precomputed_tracks or precomputed_artists or precomputed_albums)
            )
            if precomputed_ranked_hit:
                tracks = precomputed_tracks[:limit]
                artists = precomputed_artists[: max(1, min(8, limit))]
                albums = precomputed_albums[: max(1, min(8, limit))]
                ranking_summary = dict(ranked_snapshot.get("ranking_summary") or {})
            else:
                track_first_mode = query_intent == "track"
                if track_first_mode:
                    tracks = self._rank_track_candidates_fast_path(
                        req,
                        retrieval_payload,
                        limit=limit,
                    )
                    side_limit = max(1, min(4, limit // 3 if limit > 3 else 2))
                    artists = self._materialize_side_candidates(
                        retrieval_payload,
                        candidate_key="artist_candidates",
                        limit=side_limit,
                    )
                    albums = self._materialize_side_candidates(
                        retrieval_payload,
                        candidate_key="album_candidates",
                        limit=side_limit,
                    )
                else:
                    ranking_runtime = build_search_ranking_runtime(
                        server,
                        req,
                        profile,
                        retrieval_payload,
                    )
                    tracks = self._rank_track_candidates(
                        req,
                        profile,
                        retrieval_payload,
                        limit=limit,
                        ranking_runtime=ranking_runtime,
                    )
                    artists = self._rank_artist_candidates(
                        req,
                        profile,
                        retrieval_payload,
                        limit=max(1, min(12, limit)),
                        ranking_runtime=ranking_runtime,
                    )
                    albums = self._rank_album_candidates(
                        req,
                        profile,
                        retrieval_payload,
                        limit=max(1, min(12, limit)),
                        ranking_runtime=ranking_runtime,
                    )
                ranking_summary = summarize_ranked_results(
                    server,
                    tracks=tracks[:limit],
                    artists=artists[: max(1, min(8, limit))],
                    albums=albums[: max(1, min(8, limit))],
                )
                if track_first_mode:
                    ranking_summary["side_surface_mode"] = "track_first"
                    ranking_summary["track_ranking_mode"] = "lexical_fast"
            print(
                "[EBB:search][progress] "
                f"request_id={trace.get('request_id') or ''} "
                f"stage=ranking done tracks={len(tracks)} artists={len(artists)} albums={len(albums)}",
                flush=True,
            )
            cache_search_payload(tracks=tracks[:12], artists=artists[:8], albums=albums[:8])
            top_result = None
            if tracks:
                top_result = {"entity_type": "track", "item": tracks[0]}
            elif artists:
                top_result = {"entity_type": "artist", "item": artists[0]}
            elif albums:
                top_result = {"entity_type": "album", "item": albums[0]}
            server._trace_put(trace, "candidate_counts", "search.tracks", len(tracks))
            server._trace_put(trace, "candidate_counts", "search.artists", len(artists))
            server._trace_put(trace, "candidate_counts", "search.albums", len(albums))
            response = {
                "status": "success",
                "request_id": trace["request_id"],
                "model_version": track_model_version,
                "query_intent": query_intent,
                "top_result": top_result,
                "results": tracks[:limit],
                "tracks": tracks[:limit],
                "artists": artists[: max(1, min(8, limit))],
                "albums": albums[: max(1, min(8, limit))],
                "similar_artists": artists[:4] if tracks else [],
                "diagnostics": {
                    "ranking_backend": "search_service_v41",
                    "query_intent": query_intent,
                    "precomputed_ranked_hit": precomputed_ranked_hit,
                    "profile_cache_hit": bool((profile.get("profile_runtime") or {}).get("cache_hit")),
                    "profile_cache_source": (profile.get("profile_runtime") or {}).get("source") or "",
                    "catalog_feature_version": profile.get("catalog_feature_version") or "",
                    "taste_profile_version": profile.get("taste_profile_version") or "",
                    "scene_graph_version": profile.get("scene_graph_version") or "",
                    "feature_source": profile.get("feature_source") or "",
                    "negative_feedback_applied": bool(profile.get("negative_feedback_applied")),
                    "retriever_counts": retrieval_payload.get("retriever_counts") or {},
                    "retrieval": retrieval_payload.get("retrieval_diagnostics") or {},
                    "ranking_summary": ranking_summary,
                    "nearline_precompute": {
                        "hit": precompute_hit,
                        "stale": precompute_stale,
                        "resolved_from": (precompute_snapshot or {}).get("resolved_from") or "",
                        "snapshot_generated_at": float((precompute_snapshot or {}).get("generated_at") or 0.0),
                        "snapshot_expires_at": float((precompute_snapshot or {}).get("expires_at") or 0.0),
                        "runtime": precompute_runtime_snapshot(),
                    },
                },
            }
            response["diagnostics"]["request_ms"] = int(
                (time.perf_counter() - request_started_at) * 1000
            )
            response["diagnostics"].update(
                server._trace_diagnostics(server._trace_finalize(trace, status="success"))
            )
            server._trace_log_request(
                trace,
                request_type="search",
                user_scope_id=req.user_scope_id or "guest",
                model_version=track_model_version,
            )
            return response
        except Exception as exc:
            server._trace_finalize(trace, status="failed", error=str(exc))
            server._trace_log_request(
                trace,
                request_type="search",
                user_scope_id=req.user_scope_id or "guest",
                model_version=track_model_version,
            )
            raise

    def search_albums(self, req):
        response = self.search(req)
        diagnostics = dict(response.get("diagnostics") or {})
        return {
            "status": "success",
            "request_id": response.get("request_id") or "",
            "model_version": self._server._ranking_model_version("search_album_reranker_v2"),
            "albums": list(response.get("albums") or [])[: max(1, min(req.limit or 12, 12))],
            "diagnostics": diagnostics,
        }

    def search_artists(self, req):
        response = self.search(req)
        diagnostics = dict(response.get("diagnostics") or {})
        return {
            "status": "success",
            "request_id": response.get("request_id") or "",
            "model_version": self._server._ranking_model_version("search_artist_reranker_v2"),
            "artists": list(response.get("artists") or [])[: max(1, min(req.limit or 12, 12))],
            "diagnostics": diagnostics,
        }

    def suggest(self, req):
        server = self._server
        try:
            results = semantic_search_suggestions(req)
            normalized_query = server._recommendation_trim_text(req.query)
            if len(normalized_query) >= 3:
                schedule_search_warmup(
                    user_scope_id=req.user_scope_id or "guest",
                    query=normalized_query,
                )
            return {
                "status": "success",
                "results": results[: max(1, min(req.limit or 5, 8))],
                "diagnostics": {
                    "ranking_backend": "search_service_v41",
                    "warmup_scheduled": len(normalized_query) >= 3,
                },
            }
        except Exception:
            return {
                "status": "success",
                "results": [],
                "error_message": "Suggestions are temporarily unavailable.",
                "diagnostics": {"ranking_backend": "search_service_v41"},
            }
