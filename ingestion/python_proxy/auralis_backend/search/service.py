from __future__ import annotations

import os
import re
import time
from collections import Counter
from concurrent.futures import wait
from typing import Any
from typing import Dict, List

from ..domain.catalog import (
    cache_search_payload,
    catalog_source_authority,
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
    semantic_search_suggestions,
)
from .server_adapter import SearchServerAdapter
from .upstream_runtime import ytdlp_song_search

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
DIRECT_TRACK_RESCUE_BUDGET_SECONDS = max(
    0.35,
    float(os.environ.get("AURALIS_SEARCH_DIRECT_RESCUE_BUDGET_SECONDS", "3.2")),
)


class SearchService:
    def __init__(self, server: Any) -> None:
        self._server = server

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
    ) -> List[Dict[str, Any]]:
        search = self._search_server()
        normalized_query = normalize_track_title(query)
        canonical_artist_hint = self._canonical_direct_artist_hint(query, tracks)
        exact_title_artists = Counter(
            self._direct_track_artist_key(query, track)
            for track in tracks
            if normalize_track_title(track.get("title") or "") == normalized_query
        )
        exact_title_artists.pop("", None)

        def score(indexed_track):
            index, track = indexed_track
            title = normalize_track_title(track.get("title") or "")
            artist = self._direct_track_artist_key(query, track)
            album = search.normalize_text(track.get("album") or "")
            exact_title = title == normalized_query
            authority_score = self._direct_track_authority_score(track)
            popularity_score = self._direct_track_popularity_score(track)
            official_artist_score = self._direct_track_official_artist_score(track)
            match_score = self._direct_track_match_score(query, track)
            self_labeled_official_penalty = (
                -1.2 if self._direct_track_self_labeled_official(track) else 0.0
            )
            partial_title_penalty = 0.0
            if normalized_query and title != normalized_query and normalized_query in title:
                query_tokens = search.query_tokens(query)
                if len(query_tokens) <= 1:
                    partial_title_penalty = -0.45
            canonical_artist_score = self._direct_track_artist_hint_score(
                query,
                track,
                canonical_artist_hint,
            )
            return (
                match_score,
                canonical_artist_score,
                authority_score,
                official_artist_score,
                self_labeled_official_penalty,
                partial_title_penalty,
                popularity_score,
                1 if exact_title and album == normalized_query else 0,
                min(exact_title_artists.get(artist, 0), 3) if exact_title else 0,
                1 if exact_title else 0,
                -index,
            )

        return [
            dict(track)
            for _index, track in sorted(
                enumerate(tracks),
                key=score,
                reverse=True,
            )
        ]

    def _direct_track_authority_score(self, track: Dict[str, Any]) -> float:
        authority = catalog_source_authority(track)
        if authority == "search_only":
            return -4.0
        if authority == "official":
            if self._direct_track_self_labeled_official(track):
                return -0.15
            return 1.4
        if authority == "canonical":
            return 1.15
        if authority == "verified_catalog":
            return 0.65
        return -0.35

    def _direct_track_has_authoritative_source_marker(self, track: Dict[str, Any]) -> bool:
        search = self._search_server()
        text = search.normalize_text(
            " ".join(
                str(track.get(key) or "")
                for key in (
                    "artist",
                    "channel",
                    "author",
                    "uploader",
                    "uploader_id",
                    "description",
                    "source",
                    "source_type",
                )
            )
        )
        if any(
            str(track.get(key) or "").strip()
            for key in (
                "album_id",
                "albumId",
                "browseId",
                "artist_id",
                "artistId",
                "channel_id",
                "channelId",
            )
        ):
            return True
        return any(
            marker in text
            for marker in (
                "vevo",
                " topic",
                "- topic",
                "provided to youtube",
                "auto generated by youtube",
                "auto-generated by youtube",
            )
        )

    def _direct_track_self_labeled_official(self, track: Dict[str, Any]) -> bool:
        search = self._search_server()
        text = search.normalize_text(
            " ".join(
                str(track.get(key) or "")
                for key in (
                    "artist",
                    "channel",
                    "author",
                    "uploader",
                    "uploader_id",
                    "description",
                )
            )
        )
        return "official" in text and not self._direct_track_has_authoritative_source_marker(track)

    def _direct_track_popularity_score(self, track: Dict[str, Any]) -> float:
        for key in ("popularity", "view_count", "viewCount", "views", "play_count", "playCount"):
            value = track.get(key)
            try:
                number = float(value or 0.0)
            except (TypeError, ValueError):
                number = 0.0
            if number <= 0:
                continue
            if number <= 1.0:
                return number
            return min(1.0, max(0.0, (len(str(int(number))) - 3) / 7.0))
        text = search_text = self._search_server().normalize_text(
            " ".join(
                str(track.get(key) or "")
                for key in ("title", "artist", "channel", "author", "album", "description")
            )
        )
        if "official" in text or "vevo" in search_text or "topic" in search_text:
            return 0.72
        return 0.0

    def _direct_track_official_artist_score(self, track: Dict[str, Any]) -> float:
        search = self._search_server()
        text = search.normalize_text(
            " ".join(
                str(track.get(key) or "")
                for key in ("artist", "channel", "author", "uploader", "uploader_id")
            )
        )
        if not text:
            return 0.0
        score = 0.0
        if "vevo" in text:
            score += 1.35
        if "official" in text:
            score += 0.15 if self._direct_track_self_labeled_official(track) else 0.95
        if "topic" in text:
            score += 0.9
        if any(token in text for token in ("cover", "karaoke", "tribute", "piano", "instrumental")):
            score -= 1.25
        if self._direct_track_self_labeled_official(track):
            score -= 0.35
        return score

    def _direct_track_artist_key(self, query: str, track: Dict[str, Any] | None) -> str:
        if not isinstance(track, dict):
            return ""
        search = self._search_server()
        normalized_query = normalize_track_title(query)
        raw_title = str(track.get("title") or track.get("name") or "")
        title_parts = re.split(r"\s+[-–—]\s+", raw_title, maxsplit=1)
        if len(title_parts) == 2 and normalized_query:
            prefix = normalize_track_title(title_parts[0])
            suffix = normalize_track_title(title_parts[1])
            if prefix and (suffix == normalized_query or normalized_query in suffix):
                return prefix
        artist = search.normalize_text(
            track.get("channel")
            or track.get("artist")
            or track.get("author")
            or track.get("uploader")
            or ""
        )
        if not artist:
            return ""
        artist = re.sub(r"\b(official|topic|vevo|records|recordings|music)\b", " ", artist)
        artist = re.sub(r"\s+", " ", artist).strip()
        return artist

    def _compact_key(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", self._search_server().normalize_text(value))

    def _direct_track_artist_hint_score(
        self,
        query: str,
        track: Dict[str, Any],
        artist_hint: str,
    ) -> float:
        if not artist_hint:
            return 0.0
        artist_key = self._direct_track_artist_key(query, track)
        if not artist_key:
            return 0.0
        if artist_key == artist_hint:
            return 2.6
        compact_artist = self._compact_key(artist_key)
        compact_hint = self._compact_key(artist_hint)
        if compact_artist and compact_hint and compact_artist == compact_hint:
            return 2.35
        if compact_artist and compact_hint and (
            compact_artist in compact_hint or compact_hint in compact_artist
        ):
            return 1.35
        return 0.0

    def _canonical_direct_artist_hint(
        self,
        query: str,
        tracks: List[Dict[str, Any]],
    ) -> str:
        votes: Dict[str, float] = {}
        normalized_query = normalize_track_title(query)
        if not normalized_query:
            return ""
        for index, track in enumerate(tracks or []):
            if not isinstance(track, dict):
                continue
            match_score = self._direct_track_match_score(query, track)
            if match_score < 0.9:
                continue
            artist_key = self._direct_track_artist_key(query, track)
            if not artist_key:
                continue
            vote = (
                1.0
                + (match_score * 1.1)
                + max(self._direct_track_authority_score(track), 0.0)
                + max(self._direct_track_official_artist_score(track), 0.0)
                + (self._direct_track_popularity_score(track) * 0.9)
                + max(0.0, 0.7 - (index * 0.05))
            )
            title = normalize_track_title(track.get("title") or "")
            if title == normalized_query:
                vote += 0.35
            if self._direct_track_authority_score(track) < -1.0:
                vote -= 1.2
            votes[artist_key] = votes.get(artist_key, 0.0) + max(vote, 0.0)
        if not votes:
            return ""
        ranked = sorted(votes.items(), key=lambda item: item[1], reverse=True)
        if len(ranked) == 1:
            return ranked[0][0] if ranked[0][1] >= 2.2 else ""
        best_artist, best_score = ranked[0]
        second_score = ranked[1][1]
        if best_score >= 3.2 and best_score >= second_score * 1.15:
            return best_artist
        return ""

    def _has_trusted_exact_direct_track(
        self,
        query: str,
        tracks: List[Dict[str, Any]],
    ) -> bool:
        normalized_query = normalize_track_title(query)
        if not normalized_query:
            return False
        for track in tracks or []:
            if normalize_track_title(track.get("title") or "") != normalized_query:
                continue
            if self._direct_track_authority_score(track) >= 1.0:
                return True
            if self._direct_track_popularity_score(track) >= 0.72:
                return True
        return False

    def _direct_track_exact_title_diversity(
        self,
        query: str,
        tracks: List[Dict[str, Any]],
    ) -> int:
        search = self._search_server()
        normalized_query = normalize_track_title(query)
        artists = {
            search.normalize_text(track.get("channel") or track.get("artist") or "")
            for track in tracks or []
            if normalize_track_title(track.get("title") or "") == normalized_query
        }
        artists.discard("")
        return len(artists)

    def _needs_direct_track_rescue(
        self,
        query: str,
        tracks: List[Dict[str, Any]],
    ) -> bool:
        if not tracks:
            return False
        top_track = tracks[0]
        if self._direct_track_match_score(query, top_track) < 0.92:
            return False
        if self._direct_track_exact_title_diversity(query, tracks[:12]) >= 2:
            return True
        normalized_query = normalize_track_title(query)
        normalized_title = normalize_track_title(top_track.get("title") or "")
        query_tokens = self._search_server().query_tokens(query)
        if (
            len(query_tokens) <= 1
            and normalized_query
            and normalized_title != normalized_query
            and normalized_query in normalized_title
        ):
            return True
        if self._direct_track_self_labeled_official(top_track):
            return True
        return False

    def _direct_fast_path_confident(
        self,
        query: str,
        tracks: List[Dict[str, Any]],
        *,
        direct_match_score: float,
    ) -> bool:
        if not tracks or direct_match_score < 0.72:
            return False
        if self._direct_track_exact_title_diversity(query, tracks[:12]) >= 2:
            top_track = tracks[0]
            return (
                self._direct_track_authority_score(top_track) >= 1.0
                or self._direct_track_official_artist_score(top_track) >= 0.9
                or self._direct_track_popularity_score(top_track) >= 0.78
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
                self._direct_track_authority_score(tracks[0]) >= 1.0
                or self._direct_track_popularity_score(tracks[0]) >= 0.78
            )
        if direct_match_score >= 0.96:
            return not self._direct_track_self_labeled_official(tracks[0])
        if direct_match_score >= 0.92:
            return self._has_trusted_exact_direct_track(query, tracks[:8])
        return self._direct_track_authority_score(tracks[0]) >= 0.65

    def _rescue_direct_tracks(
        self,
        query: str,
        tracks: List[Dict[str, Any]],
        *,
        limit: int,
    ) -> List[Dict[str, Any]]:
        variants = [
            f"{query} official",
            f"{query} original",
            f"{query} music video",
            f"{query} official audio",
            f"{query} official music video",
            f"{query} vevo",
            f"{query} artist",
        ]
        normalized_query = self._search_server().normalize_text(query)
        if normalized_query.endswith("in") and not normalized_query.endswith("ing"):
            variants.extend(
                [
                    f"{query}g",
                    f"{query}g official",
                    f"{query}g song",
                ]
            )
        executor = self._search_executor()
        if executor is None:
            return tracks
        futures = {}
        for variant in variants:
            futures[
                executor.submit(
                    search_tracks_direct,
                    variant,
                    max(limit, 12),
                    server=self._server,
                )
            ] = f"ytmusic:{variant}"
            futures[
                executor.submit(
                    ytdlp_song_search,
                    self._server,
                    variant,
                    max(limit, 12),
                )
            ] = f"youtube:{variant}"
        done, pending = wait(set(futures), timeout=DIRECT_TRACK_RESCUE_BUDGET_SECONDS)
        for future in pending:
            future.cancel()
        rescued = list(tracks or [])
        for future in done:
            try:
                rescued.extend(list(future.result() or []))
            except Exception:
                continue
        deduped: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for track in rescued:
            if not isinstance(track, dict):
                continue
            track_id = str(track.get("id") or track.get("videoId") or "").strip()
            identity = track_id or self._search_server().normalize_text(
                f"{track.get('title') or ''}|{track.get('artist') or track.get('channel') or ''}"
            )
            if not identity or identity in seen:
                continue
            seen.add(identity)
            deduped.append(dict(track))
        canonicalized = self._canonicalize_direct_tracks(query, deduped)
        if self._direct_track_exact_title_diversity(query, canonicalized) >= 2:
            canonicalized = sorted(
                canonicalized,
                key=lambda track: (
                    self._direct_track_authority_score(track),
                    self._direct_track_official_artist_score(track),
                    self._direct_track_popularity_score(track),
                    self._direct_track_match_score(query, track),
                ),
                reverse=True,
            )
        return canonicalized[: max(limit, 16)]

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
                "ranking_backend": "search_service_direct_v1",
                "query_mode": self._resolve_search_mode(
                    query,
                    intent_hint="track",
                    explicit_mode=str(getattr(req, "search_mode", "") or ""),
                ),
                "query_intent": "track",
                "direct_track_fast_path": True,
                "direct_lookup_ms": direct_lookup_ms,
                "direct_match_score": round(direct_match_score, 4),
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
                    direct_tracks = search_tracks_direct(
                        query,
                        max(limit, 8),
                        server=server,
                    )
                    direct_tracks = self._canonicalize_direct_tracks(
                        query,
                        direct_tracks,
                    )
                    rescued_direct = False
                    if self._needs_direct_track_rescue(query, direct_tracks):
                        direct_tracks = self._rescue_direct_tracks(
                            query,
                            direct_tracks,
                            limit=limit,
                        )
                        rescued_direct = True
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
            results = semantic_search_suggestions(req, server=server)
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
