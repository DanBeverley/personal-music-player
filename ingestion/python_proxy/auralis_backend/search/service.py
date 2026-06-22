from __future__ import annotations

import os
import re
import time
from concurrent.futures import wait
from typing import Any
from typing import Dict, List

from ..domain.catalog import (
    cache_search_payload,
    normalize_artist_name,
    normalize_track_title,
)
from ..domain.features import build_search_profile
from ..domain.retrieval import retrieve_search_candidates_fast
from ..recommend.precompute import (
    build_search_snapshot,
    get_search_snapshot,
    get_search_snapshot_for_profile,
    runtime_snapshot as precompute_runtime_snapshot,
)
from ..recommend.feature_store import request_store_runtime
from ..recommend.warmup_runtime import schedule_search_warmup
from .pipeline import (
    build_search_ranking_runtime,
    rank_album_candidates,
    rank_artist_candidates,
    rank_track_candidates,
    rank_track_candidates_fast_path,
    summarize_ranked_results,
)
from .query_mode import resolve_search_mode
from .runtime import (
    search_albums_direct,
    search_artists_direct_cached,
    search_canonical_album_for_track,
    search_query_intent,
    search_tracks_direct,
    semantic_search_suggestion_items,
)
from .server_adapter import SearchServerAdapter
from .canonical import (
    has_trusted_exact_source,
    resolve_canonical_tracks,
    source_artist_key,
    source_exact_title_diversity,
    source_is_self_labeled_official,
    source_official_artist_score,
    source_popularity_score,
    source_quality_score,
)
from .catalog_pipeline import (
    catalog_playable_tracks_for_query,
    enqueue_external_catalog_seeds,
    run_external_catalog_import,
    schedule_catalog_population,
)
from .intelligence import (
    annotate_source_identities,
    load_catalog_entity_memories,
    load_query_aliases,
    load_query_memory,
    remember_candidate_observations,
    remember_catalog_entity,
    remember_search_resolution,
    remember_source_identities,
)

SEARCH_DISABLE_RANKING_PIPELINE = (
    os.environ.get("AURALIS_SEARCH_DISABLE_RANKING_PIPELINE", "0").strip().lower()
    in {"1", "true", "yes", "on"}
)
TRACK_ENRICHMENT_MIN_SCORE = 0.72
TRACK_ENRICHMENT_SIDE_LIMIT = 12
TRACK_ENRICHMENT_BUDGET_SECONDS = max(
    0.25,
    float(os.environ.get("AURALIS_SEARCH_ENRICHMENT_BUDGET_SECONDS", "3.5")),
)
DIRECT_SIDE_SURFACE_BUDGET_SECONDS = max(
    0.25,
    float(os.environ.get("AURALIS_SEARCH_SIDE_SURFACE_BUDGET_SECONDS", "2.8")),
)
SEARCH_MUSICBRAINZ_ENRICHMENT_ENABLED = (
    os.environ.get("AURALIS_SEARCH_MUSICBRAINZ_ENRICHMENT", "1").strip().lower()
    in {"1", "true", "yes", "on"}
)


class SearchService:
    def __init__(self, server: Any) -> None:
        self._server = server
        self._musicbrainz_attempted_queries: dict[str, float] = {}

    def _search_server(self) -> SearchServerAdapter:
        return SearchServerAdapter(self._server)

    def _search_executor(self):
        return getattr(self._server, "search_executor", None) or getattr(
            self._server,
            "recommendation_executor",
        )

    def _with_search_mode(self, req: Any, search_mode: str):
        normalized_mode = str(search_mode or "").strip().lower()
        if not normalized_mode:
            return req
        current_mode = str(getattr(req, "search_mode", "") or "").strip().lower()
        if current_mode == normalized_mode:
            return req
        model_copy = getattr(req, "model_copy", None)
        if callable(model_copy):
            return model_copy(update={"search_mode": normalized_mode})
        try:
            setattr(req, "search_mode", normalized_mode)
        except Exception:
            return req
        return req

    def _maybe_enrich_query_with_musicbrainz(
        self,
        query: str,
        *,
        user_scope_id: str,
    ) -> Dict[str, Any]:
        if not SEARCH_MUSICBRAINZ_ENRICHMENT_ENABLED:
            return {"attempted": False, "disabled": True}
        query_key = normalize_track_title(query)
        if not query_key:
            return {"attempted": False, "reason": "empty_query"}
        has_track_memory = bool(load_catalog_entity_memories(
            self._server,
            query=query,
            entity_type="track",
            limit=1,
        ))
        has_artist_memory = bool(load_catalog_entity_memories(
            self._server,
            query=query,
            entity_type="artist",
            limit=1,
        ))
        has_album_memory = bool(load_catalog_entity_memories(
            self._server,
            query=query,
            entity_type="album",
            limit=1,
        ))
        if has_track_memory and has_artist_memory and has_album_memory:
            return {"attempted": False, "reason": "catalog_memory_hit"}
        now = time.time()
        last_attempt = self._musicbrainz_attempted_queries.get(query_key, 0.0)
        if now - last_attempt < 3600:
            return {"attempted": False, "reason": "recently_attempted"}
        self._musicbrainz_attempted_queries[query_key] = now
        executor = self._search_executor()
        enqueue_external_catalog_seeds(
            self._server,
            [{"query": query, "seed_type": "live_search_query", "priority": 1.0}],
            user_scope_id=user_scope_id,
            provider="musicbrainz",
            source="live_search_query",
        )
        if executor is not None:
            try:
                executor.submit(
                    run_external_catalog_import,
                    self._server,
                    user_scope_id=user_scope_id,
                    provider="musicbrainz",
                    batch_size=2,
                )
            except Exception:
                pass
        return {
            "attempted": False,
            "queued": True,
            "background_continues": executor is not None,
            "foreground": False,
        }

    def _should_try_direct_track_path(self, query: str, *, intent_hint: str) -> bool:
        server = self._search_server()
        normalized_query = server.normalize_text(query)
        if not normalized_query:
            return False
        if intent_hint == "track":
            return True
        if any(
            token in normalized_query
            for token in (" album ", " artist ", " band ", " singer ", " soundtrack ", " ost ")
        ):
            return False
        query_tokens = server.query_tokens(query)
        return 1 <= len(query_tokens) <= 7

    def _resolve_search_mode(
        self,
        query: str,
        *,
        intent_hint: str,
        explicit_mode: str = "",
    ) -> str:
        return resolve_search_mode(
            query,
            normalize_text_fn=self._search_server().normalize_text,
            intent_hint=intent_hint,
            explicit_mode=explicit_mode,
        )

    def _direct_track_match_score(
        self,
        query: str,
        track: Dict[str, Any] | None,
    ) -> float:
        server = self._search_server()
        if not isinstance(track, dict):
            return 0.0
        normalized_query = normalize_track_title(query)
        normalized_title = normalize_track_title(track.get("title") or "")
        if not normalized_query or not normalized_title:
            return 0.0
        if normalized_query == normalized_title:
            return 1.0
        if normalized_query in normalized_title:
            return 0.96
        if normalized_title in normalized_query:
            normalized_artist = server.normalize_text(
                track.get("channel") or track.get("artist") or ""
            )
            return 0.94 if normalized_artist and normalized_artist in normalized_query else 0.82
        query_tokens = set(server.query_tokens(query))
        title_tokens = set(server.query_tokens(track.get("title") or ""))
        if not query_tokens or not title_tokens:
            return 0.0
        overlap = len(query_tokens & title_tokens)
        return overlap / max(len(query_tokens), 1)

    def _canonicalize_direct_tracks(
        self,
        query: str,
        tracks: List[Dict[str, Any]],
        *,
        user_scope_id: str = "guest",
    ) -> List[Dict[str, Any]]:
        catalog_tracks = catalog_playable_tracks_for_query(
            self._server,
            user_scope_id=user_scope_id,
            query=query,
            limit=4,
        )
        merged_tracks: List[Dict[str, Any]] = []
        seen_track_ids: set[str] = set()
        for track in [*catalog_tracks, *(tracks or [])]:
            if not isinstance(track, dict):
                continue
            track_id = str(track.get("id") or track.get("videoId") or "").strip()
            identity = track_id or self._search_server().normalize_text(
                f"{track.get('title') or ''}|{track.get('artist') or track.get('channel') or ''}"
            )
            if not identity or identity in seen_track_ids:
                continue
            seen_track_ids.add(identity)
            merged_tracks.append(dict(track))
        query_memory = load_query_memory(
            self._server,
            user_scope_id=user_scope_id,
            query=query,
        )
        query_memory.extend(load_query_aliases(self._server, query=query))
        query_memory.extend(
            load_catalog_entity_memories(
                self._server,
                query=query,
                entity_type="track",
            )
        )
        remember_source_identities(
            self._server,
            merged_tracks,
            confidence_floor=0.55,
            limit=32,
        )
        annotated_tracks = annotate_source_identities(self._server, merged_tracks)
        canonical = resolve_canonical_tracks(
            self._search_server(),
            query,
            annotated_tracks,
            limit=max(len(merged_tracks), 1),
            memories=query_memory,
        )
        self._remember_playable_catalog_matches(
            query,
            canonical.tracks,
            memories=query_memory,
            user_scope_id=user_scope_id,
        )
        return canonical.tracks

    def _remember_playable_catalog_matches(
        self,
        query: str,
        tracks: List[Dict[str, Any]],
        *,
        memories: List[Dict[str, Any]],
        user_scope_id: str,
    ) -> int:
        verified_memories = [
            memory
            for memory in memories or []
            if isinstance(memory, dict)
            and str(memory.get("entity_type") or "track") == "track"
            and memory.get("title_key")
            and memory.get("artist_key")
            and (
                str((memory.get("payload") or {}).get("source_provider") or "").lower()
                == "musicbrainz"
                or str((memory.get("payload") or {}).get("source_authority") or "").lower()
                in {"verified_catalog", "canonical"}
            )
        ]
        if not verified_memories:
            return 0
        stored = 0
        query_key = normalize_track_title(query)
        for track in (tracks or [])[:6]:
            if not isinstance(track, dict):
                continue
            provider = str(track.get("source_provider") or track.get("provider") or "").lower()
            track_id = str(track.get("id") or track.get("videoId") or "").strip()
            if provider == "musicbrainz" or track_id.startswith("musicbrainz:"):
                continue
            title_key = normalize_track_title(track.get("title") or track.get("name") or "")
            artist_key = normalize_artist_name(
                source_artist_key(self._search_server(), query, track)
                or track.get("artist")
                or track.get("artist_name")
                or track.get("channel")
                or track.get("author")
                or ""
            )
            if not title_key or not artist_key:
                continue
            source_quality = source_quality_score(self._search_server(), track)
            if source_quality < 0.7:
                continue
            for memory in verified_memories:
                expected_title = str(memory.get("title_key") or "")
                expected_artist = str(memory.get("artist_key") or "")
                title_match = (
                    title_key == expected_title
                    or expected_title in title_key
                    or (query_key and query_key == expected_title and query_key in title_key)
                )
                artist_match = (
                    artist_key == expected_artist
                    or expected_artist in artist_key
                    or artist_key in expected_artist
                )
                if not title_match or not artist_match:
                    continue
                item = dict(track)
                item["playable"] = True
                item["source_provider"] = provider or "ytmusic"
                item["matched_catalog_entity_key"] = memory.get("entity_key") or ""
                item["matched_catalog_source"] = "musicbrainz"
                confidence = max(
                    0.76,
                    min(0.96, float(memory.get("confidence") or 0.0) + 0.08),
                    min(0.94, 0.62 + source_quality * 0.22),
                )
                if remember_catalog_entity(
                    self._server,
                    user_scope_id=user_scope_id or "guest",
                    query=query,
                    entity_type="track",
                    item=item,
                    confidence=confidence,
                    event_weight=max(0.5, source_quality),
                    event_type="playable_source_match",
                    source="playable_source_match",
                ):
                    stored += 1
                break
        return stored

    def _direct_fast_path_confident(
        self,
        query: str,
        tracks: List[Dict[str, Any]],
        *,
        direct_match_score: float,
    ) -> bool:
        if not tracks or direct_match_score < 0.72:
            return False
        search_server = self._search_server()
        if source_exact_title_diversity(search_server, query, tracks[:12]) >= 2:
            top_track = tracks[0]
            return (
                source_quality_score(search_server, top_track) >= 1.0
                or source_official_artist_score(search_server, top_track) >= 0.9
                or source_popularity_score(search_server, top_track) >= 0.78
            )
        normalized_query = normalize_track_title(query)
        top_title = normalize_track_title(tracks[0].get("title") or "")
        if (
            len(self._search_server().query_tokens(query)) <= 1
            and normalized_query
            and top_title != normalized_query
            and normalized_query in top_title
        ):
            return (
                source_quality_score(search_server, tracks[0]) >= 1.0
                or source_popularity_score(search_server, tracks[0]) >= 0.78
            )
        if direct_match_score >= 0.96:
            return not source_is_self_labeled_official(search_server, tracks[0])
        if direct_match_score >= 0.92:
            return has_trusted_exact_source(search_server, query, tracks[:8])
        if direct_match_score >= 0.82 and len(self._search_server().query_tokens(query)) <= 3:
            return True
        return source_quality_score(search_server, tracks[0]) >= 0.65

    def _direct_fast_path_rejection_reason(
        self,
        query: str,
        tracks: List[Dict[str, Any]],
        *,
        direct_match_score: float,
    ) -> str:
        if not tracks:
            return "no_direct_tracks"
        if direct_match_score < 0.72:
            return "low_title_match"
        search_server = self._search_server()
        if source_exact_title_diversity(search_server, query, tracks[:12]) >= 2:
            top_track = tracks[0]
            if (
                source_quality_score(search_server, top_track) < 1.0
                and source_official_artist_score(search_server, top_track) < 0.9
                and source_popularity_score(search_server, top_track) < 0.78
                and direct_match_score < 0.98
            ):
                return "ambiguous_exact_title_without_authority"
        normalized_query = normalize_track_title(query)
        top_title = normalize_track_title(tracks[0].get("title") or "")
        if (
            len(self._search_server().query_tokens(query)) <= 1
            and normalized_query
            and top_title != normalized_query
            and normalized_query in top_title
            and source_quality_score(search_server, tracks[0]) < 1.0
            and source_popularity_score(search_server, tracks[0]) < 0.78
        ):
            return "short_partial_title_without_authority"
        if source_is_self_labeled_official(search_server, tracks[0]):
            return "self_labeled_official_without_source_identity"
        return "not_enough_direct_confidence"

    def _should_return_ambiguous_direct_response(
        self,
        query: str,
        tracks: List[Dict[str, Any]],
        *,
        direct_match_score: float,
        rescued: bool,
        search_mode: str,
    ) -> bool:
        if not tracks or direct_match_score < 0.72:
            return False
        query_tokens = self._search_server().query_tokens(query)
        if search_mode == "exact" and len(query_tokens) <= 3:
            return True
        if rescued and len(query_tokens) <= 3 and direct_match_score >= 0.82:
            return True
        return False

    def _track_similar_tracks(
        self,
        top_track: Dict[str, Any] | None,
        *,
        limit: int,
    ) -> List[Dict[str, Any]]:
        if not isinstance(top_track, dict):
            return []
        search = self._search_server()
        track_id = search.trim_text(top_track.get("id") or top_track.get("videoId"))
        if not track_id:
            return []
        try:
            raw_tracks = search.assistant_tool_get_similar_tracks(
                track_id,
                max(4, min(10, limit)),
            )
        except Exception:
            raw_tracks = []
        similar_tracks: List[Dict[str, Any]] = []
        seen = {track_id}
        for raw_track in raw_tracks or []:
            normalized = search.normalize_track(raw_track)
            if not normalized:
                continue
            normalized_id = search.trim_text(
                normalized.get("id") or normalized.get("videoId")
            )
            if not normalized_id or normalized_id in seen:
                continue
            seen.add(normalized_id)
            similar_tracks.append(normalized)
            if len(similar_tracks) >= limit:
                break
        return similar_tracks

    def _track_enrichment(
        self,
        query: str,
        top_track: Dict[str, Any] | None,
        *,
        limit: int,
        quality_score: float | None = None,
    ) -> Dict[str, Any]:
        empty = {
            "artists": [],
            "albums": [],
            "similar_artists": [],
            "similar_tracks": [],
            "artist_tracks": [],
            "related_albums": [],
            "quality_score": 0.0,
            "applied": False,
            "completed_surfaces": [],
            "timed_out_surfaces": [],
            "elapsed_ms": 0,
        }
        if not isinstance(top_track, dict):
            return empty
        score = (
            float(quality_score)
            if quality_score is not None
            else self._direct_track_match_score(query, top_track)
        )
        empty["quality_score"] = score
        if score < TRACK_ENRICHMENT_MIN_SCORE:
            return empty

        started_at = time.perf_counter()
        search = self._search_server()
        server = self._server
        useful_limit = max(4, min(TRACK_ENRICHMENT_SIDE_LIMIT, limit))
        extracted_artist_names = search.unique_strings(
            search.extract_artist_names(top_track),
            3,
        )
        artist_name = (
            extracted_artist_names[0]
            if extracted_artist_names
            else search.trim_text(top_track.get("channel") or top_track.get("artist"))
        )
        def resolve_artist_surface() -> Dict[str, Any]:
            if not artist_name:
                return {}
            normalized_artist = search.normalize_text(artist_name)
            try:
                artist_candidates = search_artists_direct_cached(
                    artist_name,
                    max(6, useful_limit),
                    server=server,
                )
            except Exception:
                artist_candidates = []
            primary_artist: Dict[str, Any] | None = None
            for candidate in artist_candidates:
                candidate_name = search.normalize_text(candidate.get("name") or "")
                if candidate_name == normalized_artist:
                    primary_artist = dict(candidate)
                    break
            if primary_artist is None:
                for candidate in artist_candidates:
                    candidate_name = search.normalize_text(candidate.get("name") or "")
                    if (
                        normalized_artist
                        and candidate_name
                        and (
                            normalized_artist in candidate_name
                            or candidate_name in normalized_artist
                        )
                    ):
                        primary_artist = dict(candidate)
                        break
            if primary_artist is None:
                primary_artist = {"id": None, "name": artist_name}
            artist_id = search.trim_text(primary_artist.get("id"))
            artist_payload: Dict[str, Any] = {}
            if artist_id:
                try:
                    artist_payload = search.build_artist_details_payload(
                        artist_id,
                        enrich_related=True,
                    )
                except Exception:
                    artist_payload = {}
            return {
                "primary_artist": primary_artist,
                "artist_payload": artist_payload,
                "artist_candidates": artist_candidates,
            }

        executor = self._search_executor()
        futures = {
            "artist": executor.submit(resolve_artist_surface),
            "album": executor.submit(
                search_canonical_album_for_track,
                top_track,
                server=server,
            ),
            "similar_tracks": executor.submit(
                self._track_similar_tracks,
                top_track,
                limit=useful_limit,
            ),
        }
        done, pending = wait(
            set(futures.values()),
            timeout=TRACK_ENRICHMENT_BUDGET_SECONDS,
        )
        completed_surfaces: List[str] = []
        timed_out_surfaces: List[str] = []
        resolved: Dict[str, Any] = {}
        for surface, future in futures.items():
            if future not in done:
                future.cancel()
                timed_out_surfaces.append(surface)
                continue
            try:
                resolved[surface] = future.result()
                completed_surfaces.append(surface)
            except Exception:
                resolved[surface] = {} if surface == "artist" else []
                completed_surfaces.append(surface)

        artist_surface = dict(resolved.get("artist") or {})
        primary_artist = artist_surface.get("primary_artist")
        artist_payload = dict(artist_surface.get("artist_payload") or {})
        artist_candidates = list(artist_surface.get("artist_candidates") or [])
        canonical_album = resolved.get("album")
        related_albums = self._unique_albums(
            [
                album
                for album in list(artist_payload.get("albums") or [])
                if not self._same_album(album, canonical_album)
            ],
            useful_limit,
        )
        top_track_id = search.trim_text(top_track.get("id") or top_track.get("videoId"))
        artist_tracks = self._unique_tracks(
            [
                track
                for track in list(artist_payload.get("top_songs") or [])
                if search.trim_text(track.get("id") or track.get("videoId")) != top_track_id
            ],
            useful_limit,
        )
        similar_artists = self._unique_artists(
            list(artist_payload.get("related_artists") or []),
            min(6, useful_limit),
        )
        similar_tracks = self._unique_tracks(
            list(resolved.get("similar_tracks") or []),
            useful_limit,
        )
        artists = self._unique_artists(
            [
                *([primary_artist] if isinstance(primary_artist, dict) else []),
                *similar_artists,
                *artist_candidates,
            ],
            useful_limit,
        )
        albums = self._unique_albums(
            [
                *([canonical_album] if isinstance(canonical_album, dict) else []),
                *related_albums,
            ],
            useful_limit,
        )
        return {
            "artists": artists,
            "albums": albums,
            "similar_artists": similar_artists,
            "similar_tracks": similar_tracks,
            "artist_tracks": artist_tracks,
            "related_albums": related_albums,
            "quality_score": score,
            "applied": True,
            "completed_surfaces": completed_surfaces,
            "timed_out_surfaces": timed_out_surfaces,
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
        }

    def _album_identity(self, album: Dict[str, Any] | None) -> str:
        if not isinstance(album, dict):
            return ""
        search = self._search_server()
        return search.trim_text(album.get("id")) or (
            f"{search.normalize_text(album.get('title') or '')}|"
            f"{search.normalize_text(album.get('artist') or '')}"
        )

    def _same_album(
        self,
        left: Dict[str, Any] | None,
        right: Dict[str, Any] | None,
    ) -> bool:
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        search = self._search_server()
        left_id = search.trim_text(left.get("id"))
        right_id = search.trim_text(right.get("id"))
        if left_id and right_id and left_id == right_id:
            return True
        left_title = search.normalize_text(left.get("title") or "")
        right_title = search.normalize_text(right.get("title") or "")
        if not left_title or left_title != right_title:
            return False
        left_artist = search.normalize_text(left.get("artist") or "")
        right_artist = search.normalize_text(right.get("artist") or "")
        return not left_artist or not right_artist or left_artist == right_artist

    def _unique_albums(
        self,
        albums: List[Dict[str, Any]],
        limit: int,
    ) -> List[Dict[str, Any]]:
        search = self._search_server()
        results: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for album in albums:
            artist = search.normalize_text(album.get("artist") or "")
            if not artist or artist in {"unknown", "unknown artist"}:
                continue
            key = self._album_identity(album)
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(dict(album))
            if len(results) >= limit:
                break
        return results

    def _unique_artists(
        self,
        artists: List[Dict[str, Any]],
        limit: int,
    ) -> List[Dict[str, Any]]:
        search = self._search_server()
        results: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for artist in artists:
            key = search.trim_text(artist.get("id")) or search.normalize_text(
                artist.get("name") or ""
            )
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(dict(artist))
            if len(results) >= limit:
                break
        return results

    def _unique_tracks(
        self,
        tracks: List[Dict[str, Any]],
        limit: int,
    ) -> List[Dict[str, Any]]:
        search = self._search_server()
        results: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for track in tracks:
            normalized = search.normalize_track(track)
            track_id = search.trim_text((normalized or {}).get("id"))
            if not normalized or not track_id or track_id in seen:
                continue
            seen.add(track_id)
            results.append(normalized)
            if len(results) >= limit:
                break
        return results

    def _build_direct_track_response(
        self,
        *,
        req,
        trace: Dict[str, Any],
        query: str,
        limit: int,
        track_model_version: str,
        tracks: List[Dict[str, Any]],
        direct_lookup_ms: int,
        direct_match_score: float,
        direct_resolution_status: str = "confident",
        direct_rejection_reason: str = "",
        memory_confidence: float | None = None,
    ) -> Dict[str, Any]:
        server = self._server
        search = self._search_server()
        top_track = dict(tracks[0])
        artists, albums = self._cheap_direct_side_surfaces(
            tracks,
            limit=TRACK_ENRICHMENT_SIDE_LIMIT,
        )
        enrichment: Dict[str, Any] = {}
        defer_side_surfaces = bool(getattr(req, "defer_side_surfaces", False))
        if not defer_side_surfaces:
            enrichment = self._track_enrichment(
                query,
                top_track,
                limit=max(4, min(8, limit)),
                quality_score=direct_match_score,
            )
        enriched_artists = list(enrichment.get("artists") or [])
        enriched_albums = list(enrichment.get("albums") or [])
        if enriched_artists:
            artists = enriched_artists
        if enriched_albums:
            albums = enriched_albums
        similar_artists = list(enrichment.get("similar_artists") or [])
        similar_tracks = list(enrichment.get("similar_tracks") or [])
        artist_tracks = list(enrichment.get("artist_tracks") or [])
        related_albums = list(enrichment.get("related_albums") or [])
        search.trace_put(trace, "candidate_counts", "search.tracks", len(tracks))
        search.trace_put(trace, "candidate_counts", "search.artists", len(artists))
        search.trace_put(trace, "candidate_counts", "search.albums", len(albums))
        search.trace_put(trace, "candidate_counts", "search.similar_tracks", len(similar_tracks))
        search.trace_put(trace, "candidate_counts", "search.artist_tracks", len(artist_tracks))
        search.trace_put(trace, "candidate_counts", "search.related_albums", len(related_albums))
        response = {
            "status": "success",
            "request_id": trace["request_id"],
            "model_version": track_model_version,
            "query_intent": "track",
            "top_result": {
                "entity_type": "track",
                "item": top_track,
            },
            "results": tracks[:limit],
            "tracks": tracks[:limit],
            "artists": artists[: max(1, min(TRACK_ENRICHMENT_SIDE_LIMIT, limit))],
            "albums": albums[: max(1, min(TRACK_ENRICHMENT_SIDE_LIMIT, limit))],
            "similar_artists": similar_artists[:6],
            "similar_tracks": similar_tracks[: max(1, min(8, limit))],
            "artist_tracks": artist_tracks[: max(1, min(8, limit))],
            "related_albums": related_albums[: max(1, min(8, limit))],
            "diagnostics": {
                "ranking_backend": "canonical_search_direct_v1",
                "query_mode": self._resolve_search_mode(
                    query,
                    intent_hint="track",
                    explicit_mode=str(getattr(req, "search_mode", "") or ""),
                ),
                "query_intent": "track",
                "direct_track_fast_path": True,
                "direct_lookup_ms": direct_lookup_ms,
                "direct_match_score": round(direct_match_score, 4),
                "direct_resolution_status": direct_resolution_status,
                "direct_rejection_reason": direct_rejection_reason,
                "canonical_backend": "canonical_entity_resolver_v1",
                "canonical_query_key": top_track.get("canonical_track_title_key") or normalize_track_title(query),
                "canonical_resolved_title": top_track.get("canonical_track_title_key") or "",
                "canonical_resolved_artist": top_track.get("canonical_artist_key") or "",
                "canonical_entity_confidence": top_track.get("canonical_entity_confidence") or 0.0,
                "canonical_top_source_quality": top_track.get("source_quality_score") or 0.0,
                "query_memory_boost": (
                    (top_track.get("ranking_features") or {}).get("query_memory_boost") or 0.0
                ),
                "enrichment_applied": bool(enrichment.get("applied")),
                "enrichment_elapsed_ms": int(enrichment.get("elapsed_ms") or 0),
                "enrichment_completed_surfaces": list(
                    enrichment.get("completed_surfaces") or []
                ),
                "enrichment_timed_out_surfaces": list(
                    enrichment.get("timed_out_surfaces") or []
                ),
                "deferred_side_surfaces": defer_side_surfaces,
            },
        }
        response["diagnostics"]["request_ms"] = int(
            (time.perf_counter() - trace["started_at_perf"]) * 1000
        ) if "started_at_perf" in trace else direct_lookup_ms
        response["diagnostics"].update(
            search.success_diagnostics(trace)
        )
        safe_memory_confidence = (
            float(memory_confidence)
            if memory_confidence is not None
            else float(top_track.get("canonical_entity_confidence") or direct_match_score)
        )
        memory_written = False
        if safe_memory_confidence >= 0.68 and direct_resolution_status == "confident":
            memory_written = remember_search_resolution(
                self._server,
                user_scope_id=req.user_scope_id or "guest",
                query=query,
                entity_type="track",
                item=top_track,
                confidence=safe_memory_confidence,
                event_weight=1.0,
                source="direct_track_response",
            )
        observed_candidates = remember_candidate_observations(
            self._server,
            user_scope_id=req.user_scope_id or "guest",
            query=query,
            entity_type="track",
            items=tracks[: max(8, min(16, limit))],
            confidence_floor=0.72,
            limit=8,
        )
        response["diagnostics"]["query_memory_written"] = memory_written
        response["diagnostics"]["trusted_candidate_observations"] = observed_candidates
        search.trace_log_request(
            trace,
            request_type="search",
            user_scope_id=req.user_scope_id or "guest",
            model_version=track_model_version,
        )
        return response

    def _cheap_track_side_surfaces(
        self,
        track: Dict[str, Any],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        search = self._search_server()
        artist_name = search.trim_text(
            track.get("artist")
            or track.get("artist_name")
            or track.get("channel")
            or track.get("author")
        )
        artist_id = search.trim_text(
            track.get("artist_id")
            or track.get("channel_id")
            or track.get("browseId")
        )
        thumbnail = track.get("thumbnail") or track.get("image") or ""
        artists = (
            [
                {
                    "id": artist_id,
                    "name": artist_name,
                    "thumbnail": thumbnail,
                    "resolution_status": "derived_from_track",
                }
            ]
            if artist_name
            else []
        )
        album = track.get("album")
        if isinstance(album, dict):
            album_title = search.trim_text(album.get("title") or album.get("name"))
            album_id = search.trim_text(album.get("id") or album.get("browseId"))
        else:
            album_title = search.trim_text(album or track.get("album_name"))
            album_id = search.trim_text(track.get("album_id") or track.get("albumId"))
        albums = (
            [
                {
                    "id": album_id,
                    "title": album_title,
                    "artist": artist_name,
                    "thumbnail": thumbnail,
                    "resolution_status": "derived_from_track",
                }
            ]
            if album_title
            else []
        )
        return artists, albums

    def _cheap_direct_side_surfaces(
        self,
        tracks: List[Dict[str, Any]],
        *,
        limit: int,
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        artists: List[Dict[str, Any]] = []
        albums: List[Dict[str, Any]] = []
        seen_artists: set[str] = set()
        seen_albums: set[str] = set()
        search = self._search_server()
        for track in tracks or []:
            track_artists, track_albums = self._cheap_track_side_surfaces(track)
            for artist in track_artists:
                key = search.normalize_text(
                    artist.get("id") or artist.get("name") or ""
                )
                if not key or key in seen_artists:
                    continue
                seen_artists.add(key)
                artists.append(artist)
                if len(artists) >= limit:
                    break
            for album in track_albums:
                key = search.normalize_text(
                    f"{album.get('id') or ''}|{album.get('title') or ''}|{album.get('artist') or album.get('artist_name') or ''}"
                )
                if not key or key in seen_albums:
                    continue
                seen_albums.add(key)
                albums.append(album)
                if len(albums) >= limit:
                    break
            if len(artists) >= limit and len(albums) >= limit:
                break
        if not artists and tracks:
            artists, albums = self._cheap_track_side_surfaces(dict(tracks[0]))
        return artists[:limit], albums[:limit]

    def _build_direct_search_response(
        self,
        *,
        req,
        trace: Dict[str, Any],
        query_intent: str,
        limit: int,
        track_model_version: str,
        tracks: List[Dict[str, Any]],
        artists: List[Dict[str, Any]],
        albums: List[Dict[str, Any]],
        similar_artists: List[Dict[str, Any]],
        direct_lookup_ms: int,
        similar_tracks: List[Dict[str, Any]] | None = None,
        artist_tracks: List[Dict[str, Any]] | None = None,
        related_albums: List[Dict[str, Any]] | None = None,
        enrichment_applied: bool = False,
        enrichment_quality_score: float = 0.0,
        enrichment_elapsed_ms: int = 0,
        enrichment_completed_surfaces: List[str] | None = None,
        enrichment_timed_out_surfaces: List[str] | None = None,
    ) -> Dict[str, Any]:
        search = self._search_server()
        similar_tracks = list(similar_tracks or [])
        artist_tracks = list(artist_tracks or [])
        related_albums = list(related_albums or [])
        enrichment_completed_surfaces = list(enrichment_completed_surfaces or [])
        enrichment_timed_out_surfaces = list(enrichment_timed_out_surfaces or [])
        top_result = None
        if tracks:
            top_result = {"entity_type": "track", "item": tracks[0]}
        elif artists:
            top_result = {"entity_type": "artist", "item": artists[0]}
        elif albums:
            top_result = {"entity_type": "album", "item": albums[0]}
        search.trace_put(trace, "candidate_counts", "search.tracks", len(tracks))
        search.trace_put(trace, "candidate_counts", "search.artists", len(artists))
        search.trace_put(trace, "candidate_counts", "search.albums", len(albums))
        search.trace_put(trace, "candidate_counts", "search.similar_tracks", len(similar_tracks))
        search.trace_put(trace, "candidate_counts", "search.artist_tracks", len(artist_tracks))
        search.trace_put(trace, "candidate_counts", "search.related_albums", len(related_albums))
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
            "similar_artists": similar_artists[:6],
            "similar_tracks": similar_tracks[: max(1, min(8, limit))],
            "artist_tracks": artist_tracks[: max(1, min(8, limit))],
            "related_albums": related_albums[: max(1, min(8, limit))],
            "diagnostics": {
                "ranking_backend": "search_service_direct_only_v1",
                "query_mode": self._resolve_search_mode(
                    req.query,
                    intent_hint=query_intent,
                    explicit_mode=str(getattr(req, "search_mode", "") or ""),
                ),
                "query_intent": query_intent,
                "direct_search_only": True,
                "direct_lookup_ms": direct_lookup_ms,
                "enrichment_applied": enrichment_applied,
                "enrichment_quality_score": round(enrichment_quality_score, 4),
                "enrichment_elapsed_ms": int(enrichment_elapsed_ms or 0),
                "enrichment_completed_surfaces": enrichment_completed_surfaces,
                "enrichment_timed_out_surfaces": enrichment_timed_out_surfaces,
            },
        }
        response["diagnostics"]["request_ms"] = int(
            (time.perf_counter() - trace["started_at_perf"]) * 1000
        ) if "started_at_perf" in trace else direct_lookup_ms
        response["diagnostics"].update(
            search.success_diagnostics(trace)
        )
        if top_result and isinstance(top_result.get("item"), dict):
            response["diagnostics"]["query_memory_written"] = remember_search_resolution(
                self._server,
                user_scope_id=req.user_scope_id or "guest",
                query=req.query or "",
                entity_type=str(top_result.get("entity_type") or "track"),
                item=dict(top_result.get("item") or {}),
                confidence=max(0.35, float(enrichment_quality_score or 0.0)),
                event_weight=0.75,
                source="direct_search_response",
            )
        search.trace_log_request(
            trace,
            request_type="search",
            user_scope_id=req.user_scope_id or "guest",
            model_version=track_model_version,
        )
        return response

    def _search_without_ranking(
        self,
        *,
        req,
        trace: Dict[str, Any],
        query: str,
        limit: int,
        query_intent: str,
        track_model_version: str,
    ) -> Dict[str, Any]:
        server = self._server
        side_limit = max(1, min(8, limit))
        defer_side_surfaces = bool(getattr(req, "defer_side_surfaces", False))
        direct_started_at = time.perf_counter()
        tracks = list(search_tracks_direct(query, limit, server=server) or [])
        tracks = self._canonicalize_direct_tracks(
            query,
            tracks,
            user_scope_id=req.user_scope_id or "guest",
        )
        artists, albums = self._cheap_track_side_surfaces(tracks[0]) if tracks else ([], [])
        similar_artists: List[Dict[str, Any]] = []
        similar_tracks: List[Dict[str, Any]] = []
        artist_tracks: List[Dict[str, Any]] = []
        related_albums: List[Dict[str, Any]] = []
        enrichment: Dict[str, Any] = {}
        if tracks and not defer_side_surfaces:
            enrichment = self._track_enrichment(
                query,
                tracks[0],
                limit=side_limit,
            )
            enriched_artists = list(enrichment.get("artists") or [])
            enriched_albums = list(enrichment.get("albums") or [])
            if enriched_artists:
                artists = enriched_artists
            if enriched_albums:
                albums = enriched_albums
            similar_artists = list(enrichment.get("similar_artists") or [])
            similar_tracks = list(enrichment.get("similar_tracks") or [])
            artist_tracks = list(enrichment.get("artist_tracks") or [])
            related_albums = list(enrichment.get("related_albums") or [])
        else:
            search_executor = self._search_executor()
            artist_future = search_executor.submit(
                search_artists_direct_cached,
                query,
                side_limit,
                server=server,
            )
            album_future = search_executor.submit(
                search_albums_direct,
                query,
                side_limit,
                server=server,
            )
            side_futures = {
                "artists": artist_future,
                "albums": album_future,
            }
            done, pending = wait(
                set(side_futures.values()),
                timeout=DIRECT_SIDE_SURFACE_BUDGET_SECONDS,
            )
            for surface, future in side_futures.items():
                if future not in done:
                    future.cancel()
                    continue
                try:
                    values = list(future.result() or [])
                except Exception:
                    values = []
                if surface == "artists":
                    if values:
                        artists = values
                elif values:
                    albums = values
            similar_artists = []
        direct_lookup_ms = int((time.perf_counter() - direct_started_at) * 1000)
        print(
            "[EBB:search][progress] "
            f"request_id={trace.get('request_id') or ''} "
            f"stage=direct_search_only done tracks={len(tracks)} artists={len(artists)} "
            f"albums={len(albums)} lookup_ms={direct_lookup_ms}",
            flush=True,
        )
        cache_search_payload(tracks=tracks[:12], artists=artists[:8], albums=albums[:8])
        response = self._build_direct_search_response(
            req=req,
            trace=trace,
            query_intent=query_intent,
            limit=limit,
            track_model_version=track_model_version,
            tracks=tracks,
            artists=artists,
            albums=albums,
            similar_artists=similar_artists,
            direct_lookup_ms=direct_lookup_ms,
            similar_tracks=similar_tracks,
            artist_tracks=artist_tracks,
            related_albums=related_albums,
            enrichment_applied=bool(enrichment.get("applied")),
            enrichment_quality_score=float(enrichment.get("quality_score") or 0.0),
            enrichment_elapsed_ms=int(enrichment.get("elapsed_ms") or 0),
            enrichment_completed_surfaces=list(
                enrichment.get("completed_surfaces") or []
            ),
            enrichment_timed_out_surfaces=list(
                enrichment.get("timed_out_surfaces") or []
            ),
        )
        diagnostics = dict(response.get("diagnostics") or {})
        diagnostics["deferred_side_surfaces"] = defer_side_surfaces and bool(tracks)
        response["diagnostics"] = diagnostics
        return response

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
        server = self._search_server()
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
                dedupe_key = server.normalize_text(payload.get("name") or "") or server.trim_text(payload.get("id"))
            else:
                dedupe_key = (
                    server.trim_text(payload.get("id"))
                    or f"{server.normalize_text(payload.get('title') or '')}|{server.normalize_text(payload.get('artist') or '')}"
                )
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            source_scores = dict((entry or {}).get("source_scores") or {})
            source_names = sorted(
                source_name
                for source_name in source_scores.keys()
                if server.trim_text(source_name)
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
        search = self._search_server()
        trace = search.trace_start(
            "search",
            user_scope_id=req.user_scope_id or "guest",
            surface=req.surface or "home_feed",
            query=req.query or "",
        )
        request_started_at = time.perf_counter()
        query = search.trim_text(req.query)
        limit = max(8, min(req.limit or 16, 16))
        track_model_version = search.ranking_model_version("search_track_reranker_v2")
        trace["started_at_perf"] = request_started_at
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
                        "similar_tracks": [],
                        "artist_tracks": [],
                        "related_albums": [],
                        "diagnostics": {
                            "ranking_backend": "search_service_v41",
                            "empty_query": True,
                        },
                    }
                    response["diagnostics"].update(
                        search.success_diagnostics(trace)
                    )
                    search.trace_log_request(
                        trace,
                        request_type="search",
                        user_scope_id=req.user_scope_id or "guest",
                        model_version=track_model_version,
                    )
                    return response

                schedule_catalog_population(
                    self._server,
                    user_scope_id=req.user_scope_id or "guest",
                    req=req,
                    reason="search_request",
                    run_musicbrainz=False,
                    min_interval_seconds=90.0,
                )

                parse_started_at = time.perf_counter()
                url_match = re.search(
                    r"(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})",
                    query,
                )
                search.trace_stage(trace, "search.request_parse", parse_started_at)
                if url_match:
                    video_id = url_match.group(1)
                    watch = search.upstream_call_with_retry(
                        lambda: server.ytmusic.get_watch_playlist(videoId=video_id),
                        default={},
                    )
                    vd = (watch or {}).get("videoDetails", {})
                    track_payload = search.normalize_track(
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
                        "similar_tracks": [],
                        "artist_tracks": [],
                        "related_albums": [],
                        "diagnostics": {
                            "ranking_backend": "search_service_v41",
                            "url_query": True,
                        },
                    }
                    response["diagnostics"].update(
                        search.success_diagnostics(trace)
                    )
                    search.trace_log_request(
                        trace,
                        request_type="search",
                        user_scope_id=req.user_scope_id or "guest",
                        model_version=track_model_version,
                    )
                    return response

                intent_hint = search_query_intent(query, server=server)
                search_mode = self._resolve_search_mode(
                    query,
                    intent_hint=intent_hint,
                    explicit_mode=str(getattr(req, "search_mode", "") or ""),
                )
                if self._should_try_direct_track_path(query, intent_hint=intent_hint):
                    direct_started_at = time.perf_counter()
                    print(
                        "[EBB:search][progress] "
                        f"request_id={trace.get('request_id') or ''} "
                        f"stage=direct_track_fast_path start query={query[:48]} "
                        f"intent={intent_hint} mode={search_mode}",
                        flush=True,
                    )
                    musicbrainz_enrichment = self._maybe_enrich_query_with_musicbrainz(
                        query,
                        user_scope_id=req.user_scope_id or "guest",
                    )
                    if musicbrainz_enrichment.get("attempted") or musicbrainz_enrichment.get("imported"):
                        search.trace_put(
                            trace,
                            "search",
                            "musicbrainz_enrichment",
                            musicbrainz_enrichment,
                        )
                    direct_tracks = search_tracks_direct(
                        query,
                        max(limit, 8),
                        server=server,
                    )
                    direct_tracks = self._canonicalize_direct_tracks(
                        query,
                        direct_tracks,
                        user_scope_id=req.user_scope_id or "guest",
                    )
                    rescued_direct = False
                    direct_lookup_ms = int((time.perf_counter() - direct_started_at) * 1000)
                    direct_match_score = self._direct_track_match_score(
                        query,
                        direct_tracks[0] if direct_tracks else None,
                    )
                    if direct_tracks and self._direct_fast_path_confident(
                        query,
                        direct_tracks,
                        direct_match_score=direct_match_score,
                    ):
                        print(
                            "[EBB:search][progress] "
                            f"request_id={trace.get('request_id') or ''} "
                            f"stage=direct_track_fast_path done tracks={len(direct_tracks)} "
                            f"lookup_ms={direct_lookup_ms} score={round(direct_match_score, 4)} "
                            f"rescued={int(rescued_direct)}",
                            flush=True,
                        )
                        return self._build_direct_track_response(
                            req=req,
                            trace=trace,
                            query=query,
                            limit=limit,
                            track_model_version=track_model_version,
                            tracks=direct_tracks,
                            direct_lookup_ms=direct_lookup_ms,
                            direct_match_score=direct_match_score,
                            direct_resolution_status="confident",
                            memory_confidence=float(
                                (direct_tracks[0].get("canonical_entity_confidence") or direct_match_score)
                            ),
                        )
                    direct_rejection_reason = self._direct_fast_path_rejection_reason(
                        query,
                        direct_tracks,
                        direct_match_score=direct_match_score,
                    )
                    if self._should_return_ambiguous_direct_response(
                        query,
                        direct_tracks,
                        direct_match_score=direct_match_score,
                        rescued=rescued_direct,
                        search_mode=search_mode,
                    ):
                        print(
                            "[EBB:search][progress] "
                            f"request_id={trace.get('request_id') or ''} "
                            f"stage=direct_track_fast_path ambiguous tracks={len(direct_tracks)} "
                            f"lookup_ms={direct_lookup_ms} score={round(direct_match_score, 4)} "
                            f"rescued={int(rescued_direct)} reason={direct_rejection_reason}",
                            flush=True,
                        )
                        return self._build_direct_track_response(
                            req=req,
                            trace=trace,
                            query=query,
                            limit=limit,
                            track_model_version=track_model_version,
                            tracks=direct_tracks,
                            direct_lookup_ms=direct_lookup_ms,
                            direct_match_score=direct_match_score,
                            direct_resolution_status="ambiguous",
                            direct_rejection_reason=direct_rejection_reason,
                            memory_confidence=min(0.55, direct_match_score),
                        )
                    if direct_tracks:
                        observed = remember_candidate_observations(
                            self._server,
                            user_scope_id=req.user_scope_id or "guest",
                            query=query,
                            entity_type="track",
                            items=direct_tracks[: max(8, min(16, limit))],
                            confidence_floor=0.74,
                            limit=8,
                        )
                        if observed:
                            search.trace_put(
                                trace,
                                "search",
                                "trusted_candidate_observations",
                                observed,
                            )
                if SEARCH_DISABLE_RANKING_PIPELINE and search_mode == "exact":
                    response = self._search_without_ranking(
                        req=req,
                        trace=trace,
                        query=query,
                        limit=limit,
                        query_intent=intent_hint,
                        track_model_version=track_model_version,
                    )
                    diagnostics = dict(response.get("diagnostics") or {})
                    diagnostics["query_mode"] = search_mode
                    diagnostics["ranking_pipeline_mode"] = "direct_only"
                    response["diagnostics"] = diagnostics
                    return response

                profile_started_at = time.perf_counter()
                legacy_req, profile = build_search_profile(req)
                legacy_req = self._with_search_mode(legacy_req, search_mode)
                search.trace_stage(trace, "search.profile_build", profile_started_at)
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
                        server=server,
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
                        server=server,
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
                                search_mode=search_mode,
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
                            server=server,
                        )
                elif precompute_stale:
                    schedule_search_warmup(
                        server=server,
                        user_scope_id=req.user_scope_id or "guest",
                        query=query,
                        search_mode=search_mode,
                    )
                print(
                    "[EBB:search][progress] "
                    f"request_id={trace.get('request_id') or ''} "
                    f"stage=retrieval resolved_hit={precompute_hit} stale={precompute_stale} "
                    f"source={((precompute_snapshot or {}).get('resolved_from') or '')}",
                    flush=True,
                )
            search.trace_stage(trace, "search.retrieval", retrieval_started_at)
            query_intent = retrieval_payload.get("query_intent") or search_query_intent(
                query,
                server=server,
            )
            search_mode = self._resolve_search_mode(
                query,
                intent_hint=query_intent,
                explicit_mode=search_mode,
            )
            ranked_snapshot = dict((precompute_snapshot or {}).get("ranked_results") or {})
            precomputed_tracks = list(ranked_snapshot.get("tracks") or [])
            precomputed_artists = list(ranked_snapshot.get("artists") or [])
            precomputed_albums = list(ranked_snapshot.get("albums") or [])
            precomputed_ranked_hit = bool(
                precompute_hit and (precomputed_tracks or precomputed_artists or precomputed_albums)
            )
            defer_side_surfaces = bool(getattr(req, "defer_side_surfaces", False))
            track_first_mode = False
            ranking_runtime: dict[str, Any] = {}
            if precomputed_ranked_hit:
                tracks = precomputed_tracks[:limit]
                if defer_side_surfaces and tracks:
                    artists, albums = self._cheap_track_side_surfaces(tracks[0])
                else:
                    artists = precomputed_artists[: max(1, min(8, limit))]
                    albums = precomputed_albums[: max(1, min(8, limit))]
                ranking_summary = dict(ranked_snapshot.get("ranking_summary") or {})
            else:
                track_first_mode = query_intent == "track" and search_mode == "exact"
                if track_first_mode:
                    tracks = self._rank_track_candidates_fast_path(
                        req,
                        retrieval_payload,
                        limit=limit,
                    )
                    if defer_side_surfaces and tracks:
                        artists, albums = self._cheap_track_side_surfaces(tracks[0])
                    else:
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
                        search_mode=search_mode,
                        query_intent=query_intent,
                        limit=limit,
                    )
                    ranking_budget = dict(ranking_runtime.get("ranking_budget") or {})
                    tracks = self._rank_track_candidates(
                        req,
                        profile,
                        retrieval_payload,
                        limit=limit,
                        ranking_runtime=ranking_runtime,
                    )
                    if defer_side_surfaces and tracks:
                        artists, albums = self._cheap_track_side_surfaces(tracks[0])
                    else:
                        artist_limit = int(
                            ranking_budget.get("artist_output_limit")
                            or max(1, min(12, limit))
                        )
                        album_limit = int(
                            ranking_budget.get("album_output_limit")
                            or max(1, min(12, limit))
                        )
                        artists = self._rank_artist_candidates(
                            req,
                            profile,
                            retrieval_payload,
                            limit=artist_limit,
                            ranking_runtime=ranking_runtime,
                        )
                        albums = self._rank_album_candidates(
                            req,
                            profile,
                            retrieval_payload,
                            limit=album_limit,
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
            top_result = None
            if tracks:
                top_result = {"entity_type": "track", "item": tracks[0]}
            elif artists:
                top_result = {"entity_type": "artist", "item": artists[0]}
            elif albums:
                top_result = {"entity_type": "album", "item": albums[0]}
            similar_artists: List[Dict[str, Any]] = []
            similar_tracks: List[Dict[str, Any]] = []
            artist_tracks: List[Dict[str, Any]] = []
            related_albums: List[Dict[str, Any]] = []
            enrichment: Dict[str, Any] = {}
            if tracks and not defer_side_surfaces:
                enrichment = self._track_enrichment(
                    query,
                    tracks[0],
                    limit=max(4, min(8, limit)),
                )
                if enrichment.get("applied"):
                    enriched_artists = list(enrichment.get("artists") or [])
                    enriched_albums = list(enrichment.get("albums") or [])
                    if enriched_artists:
                        artists = enriched_artists
                    if enriched_albums:
                        albums = enriched_albums
                similar_artists = list(enrichment.get("similar_artists") or [])
                similar_tracks = list(enrichment.get("similar_tracks") or [])
                artist_tracks = list(enrichment.get("artist_tracks") or [])
                related_albums = list(enrichment.get("related_albums") or [])
            cache_search_payload(tracks=tracks[:12], artists=artists[:8], albums=albums[:8])
            search.trace_put(trace, "candidate_counts", "search.tracks", len(tracks))
            search.trace_put(trace, "candidate_counts", "search.artists", len(artists))
            search.trace_put(trace, "candidate_counts", "search.albums", len(albums))
            search.trace_put(trace, "candidate_counts", "search.similar_tracks", len(similar_tracks))
            search.trace_put(trace, "candidate_counts", "search.artist_tracks", len(artist_tracks))
            search.trace_put(trace, "candidate_counts", "search.related_albums", len(related_albums))
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
                "similar_artists": similar_artists[:6] if tracks else [],
                "similar_tracks": similar_tracks[: max(1, min(8, limit))],
                "artist_tracks": artist_tracks[: max(1, min(8, limit))],
                "related_albums": related_albums[: max(1, min(8, limit))],
                "diagnostics": {
                    "ranking_backend": "search_service_v41",
                    "query_mode": search_mode,
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
                    "ranking_budget": dict(ranking_runtime.get("ranking_budget") or {}),
                    "deferred_side_surfaces": defer_side_surfaces and bool(tracks),
                    "enrichment_applied": bool(enrichment.get("applied")),
                    "enrichment_quality_score": round(
                        float(enrichment.get("quality_score") or 0.0),
                        4,
                    ),
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
                search.success_diagnostics(trace)
            )
            if (
                not precomputed_ranked_hit
                and search_mode in {"entity", "taste"}
                and len(self._search_server().trim_text(query)) >= 3
            ):
                schedule_search_warmup(
                    server=server,
                    user_scope_id=req.user_scope_id or "guest",
                    query=query,
                    search_mode=search_mode,
                )
            search.trace_log_request(
                trace,
                request_type="search",
                user_scope_id=req.user_scope_id or "guest",
                model_version=track_model_version,
            )
            return response
        except Exception as exc:
            search.trace_finalize(trace, status="failed", error=str(exc))
            search.trace_log_request(
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
            "model_version": self._search_server().ranking_model_version("search_album_reranker_v2"),
            "albums": list(response.get("albums") or [])[: max(1, min(req.limit or 12, 12))],
            "diagnostics": diagnostics,
        }

    def search_artists(self, req):
        response = self.search(req)
        diagnostics = dict(response.get("diagnostics") or {})
        return {
            "status": "success",
            "request_id": response.get("request_id") or "",
            "model_version": self._search_server().ranking_model_version("search_artist_reranker_v2"),
            "artists": list(response.get("artists") or [])[: max(1, min(req.limit or 12, 12))],
            "diagnostics": diagnostics,
        }

    def resolve_artist(self, req):
        started_at = time.perf_counter()
        query = self._search_server().trim_text(req.query)
        try:
            artists = search_artists_direct_cached(
                query,
                max(1, min(req.limit or 4, 4)),
                server=self._server,
            )
            error = ""
        except Exception as exc:
            artists = []
            error = str(exc)
        normalized_query = self._search_server().normalize_text(query)
        resolved = next(
            (
                artist
                for artist in artists
                if self._search_server().normalize_text(artist.get("name")) == normalized_query
            ),
            artists[0] if artists else None,
        )
        return {
            "status": "success",
            "artist": resolved,
            "artists": artists,
            "diagnostics": {
                "ranking_backend": "canonical_artist_resolver_v1",
                "request_ms": int((time.perf_counter() - started_at) * 1000),
                "candidate_count": len(artists),
                "resolved": resolved is not None,
                "error": error,
            },
        }

    def suggest(self, req):
        server = self._server
        search = self._search_server()
        try:
            suggestion_items = semantic_search_suggestion_items(req, server=server)
            results = [
                item.get("text") if isinstance(item, dict) else str(item)
                for item in suggestion_items
                if (item.get("text") if isinstance(item, dict) else str(item))
            ]
            normalized_query = search.trim_text(req.query)
            suggestion_mode = self._resolve_search_mode(
                normalized_query,
                intent_hint=search_query_intent(normalized_query, server=server),
                explicit_mode="",
            )
            if len(normalized_query) >= 3:
                schedule_search_warmup(
                    server=server,
                    user_scope_id=req.user_scope_id or "guest",
                    query=normalized_query,
                    search_mode=suggestion_mode,
                )
            return {
                "status": "success",
                "results": results[: max(1, min(req.limit or 5, 8))],
                "suggestions": suggestion_items[: max(1, min(req.limit or 5, 8))],
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
