from __future__ import annotations

from typing import Any, Dict, List, Optional
import time

from .server_adapter import adapt_domain_server
from .features import build_recommendation_model_version
from ..search.upstream_runtime import (
    artist_names_from_track_query as resolve_artist_names_from_track_query,
    search_artists as resolve_search_artists,
    search_artists_direct as resolve_search_artists_direct,
)


def trim_text(value: Optional[str]) -> str:
    return adapt_domain_server().trim_text(value)


def _reference_vectors(anchor_tracks, anchor_artist_names, *, server: Any | None = None):
    server = adapt_domain_server(server)
    anchor_track_snapshots = server.unique_snapshot_tracks(anchor_tracks, 6)
    track_embeddings = server.recommendation_track_embeddings(anchor_track_snapshots)
    anchor_track_vectors = []
    for index, track in enumerate(anchor_track_snapshots):
        track_key = server.recommendation_track_embedding_key(track)
        track_vector = track_embeddings.get(track_key) or []
        if not track_vector:
            continue
        anchor_track_vectors.append((track_vector, max(2.1 - (index * 0.22), 0.65)))

    anchor_artist_entries = []
    for index, artist_name in enumerate(server.unique_strings(anchor_artist_names, 6)):
        text = f"artist {artist_name}"
        key = server.recommendation_text_embedding_key("recommended_artist_anchor_v2", text)
        anchor_artist_entries.append((key, text, max(2.0 - (index * 0.18), 0.6)))

    artist_embeddings = server.recommendation_embed_entries(
        "text",
        [(key, text) for key, text, _weight in anchor_artist_entries],
    )
    anchor_artist_vectors = []
    for key, _text, weight in anchor_artist_entries:
        vector = artist_embeddings.get(key) or []
        if not vector:
            continue
        anchor_artist_vectors.append((vector, weight))
    return {
        "anchor_track_vector": server.vector_weighted_average(anchor_track_vectors),
        "anchor_artist_vector": server.vector_weighted_average(anchor_artist_vectors),
    }


def _anchor_penalty(anchor_names, candidate_name: str, *, server: Any | None = None) -> float:
    server = adapt_domain_server(server)
    normalized_candidate = server.normalize_text(candidate_name)
    if not normalized_candidate:
        return 0.0
    penalty = 0.0
    for anchor_name in anchor_names or []:
        normalized_anchor = server.normalize_text(anchor_name)
        if not normalized_anchor:
            continue
        if normalized_candidate == normalized_anchor:
            return 8.0
        penalty = max(penalty, server.artist_related_name_penalty(anchor_name, candidate_name))
        if normalized_candidate.startswith(normalized_anchor) or normalized_anchor.startswith(normalized_candidate):
            penalty = max(penalty, 1.6)
    return penalty


class ArtistRecommendationService:
    def __init__(self, server: Any | None = None) -> None:
        self._server = server

    def _resolved_server(self):
        self._server = adapt_domain_server(self._server)
        return self._server

    def _executor(self):
        server = self._resolved_server()
        return getattr(server, "search_executor", None) or getattr(
            server,
            "recommendation_row_executor",
            None,
        ) or server.recommendation_executor

    def recommend(
        self,
        *,
        legacy_req,
        profile: Dict[str, Any],
        limit: int,
        anchor_tracks: Optional[List[Dict[str, Any]]] = None,
        anchor_artist_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        self._server = self._resolved_server()
        request_started_at = time.perf_counter()
        surface = trim_text(getattr(legacy_req, "surface", "") or "home_feed") or "home_feed"
        profile_vectors = profile.get("vectors") or {}
        collaborative = profile.get("collaborative") or {}
        listened_artist_names = {
            self._server.normalize_text(name)
            for name in (profile.get("listened_artists") or [])
            if self._server.normalize_text(name)
        }
        query_seeds = self._server.unique_strings(
            [
                getattr(legacy_req, "query", ""),
                *(getattr(legacy_req, "recent_queries", []) or []),
                *(getattr(legacy_req, "taste_queries", []) or []),
            ],
            8,
        )
        anchor_tracks = self._server.unique_snapshot_tracks(
            anchor_tracks or getattr(legacy_req, "anchor_track_snapshots", []) or profile.get("anchor_track_snapshots") or [],
            6,
        )
        anchor_artist_names = self._server.unique_strings(
            [
                *(anchor_artist_names or []),
                *(getattr(legacy_req, "anchor_artist_hints", []) or []),
                *(profile.get("anchor_artist_hints") or []),
                *(
                    artist_name
                    for track in anchor_tracks
                    for artist_name in self._server.extract_artist_names(track)
                ),
            ],
            8,
        )

        weighted_artist_names: Dict[str, float] = {}

        def add_artist_seed(raw_name: Optional[str], weight: float) -> None:
            text = trim_text(raw_name)
            normalized = self._server.normalize_text(text)
            if not text or not normalized:
                return
            weighted_artist_names[normalized] = max(weighted_artist_names.get(normalized, 0.0), weight)

        def add_track_artist_seeds(tracks: List[Dict[str, Any]], base_weight: float) -> None:
            for index, track in enumerate(tracks or []):
                for artist_name in self._server.extract_artist_names(track):
                    add_artist_seed(artist_name, max(base_weight - (index * 0.12), 0.45))

        if surface == "search_results":
            for index, anchor_artist in enumerate(anchor_artist_names):
                add_artist_seed(anchor_artist, max(5.0 - (index * 0.35), 2.0))
            add_track_artist_seeds(anchor_tracks, 4.6)
            if not weighted_artist_names:
                for query in query_seeds:
                    for artist_name, score in resolve_artist_names_from_track_query(self._server, query, 3):
                        add_artist_seed(artist_name, score + 1.8)
        else:
            for index, artist_hint in enumerate(profile.get("artist_hints") or []):
                add_artist_seed(artist_hint, max(3.8 - (index * 0.18), 1.6))
            add_track_artist_seeds(profile.get("last_played_tracks") or [], 4.4)
            add_track_artist_seeds(profile.get("top_track_snapshots") or [], 3.5)
            add_track_artist_seeds(profile.get("recent_track_snapshots") or [], 3.0)
            for query in query_seeds:
                for artist_name, score in resolve_artist_names_from_track_query(self._server, query, 3):
                    add_artist_seed(artist_name, score + 1.25)

        for index, item in enumerate(
            sorted((collaborative.get("artist_scores") or {}).items(), key=lambda entry: entry[1], reverse=True)[:6]
        ):
            artist_key, score = item
            if not artist_key:
                continue
            weight = max(float(score) * (0.6 if surface == "search_results" else 0.55), 1.0) - (index * 0.08)
            add_artist_seed(artist_key, weight)

        ranked_seed_names = sorted(weighted_artist_names.items(), key=lambda item: item[1], reverse=True)
        top_seed_names = ranked_seed_names[: (5 if surface == "search_results" else 4)]

        artists: List[Dict[str, Any]] = []
        seen_artist_ids = set()
        seen_artist_names = set()
        excluded_artist_names = {
            self._server.normalize_text(name)
            for name in anchor_artist_names
            if self._server.normalize_text(name)
        } if surface == "search_results" else set()

        def add_artist_result(raw_artist: Dict[str, Any], score: float) -> None:
            artist_id = trim_text((raw_artist or {}).get("id"))
            artist_name = trim_text((raw_artist or {}).get("name"))
            normalized_name = self._server.normalize_text(artist_name)
            if (
                not artist_id
                or not artist_name
                or artist_id in seen_artist_ids
                or normalized_name in seen_artist_names
                or (excluded_artist_names and normalized_name in excluded_artist_names)
            ):
                return
            artist = dict(raw_artist)
            artist["score"] = round(score, 3)
            seen_artist_ids.add(artist_id)
            seen_artist_names.add(normalized_name)
            artists.append(artist)

        direct_search_limit = 3 if surface == "search_results" else 2
        direct_seed_futures = {
            seed_index: self._executor().submit(
                resolve_search_artists_direct,
                self._server,
                seed_name,
                direct_search_limit,
            )
            for seed_index, (seed_name, _seed_weight) in enumerate(top_seed_names)
        }
        semantic_seed_futures = {}
        if surface == "search_results":
            semantic_seed_futures = {
                seed_index: self._executor().submit(
                    resolve_search_artists,
                    self._server,
                    seed_name,
                    4,
                )
                for seed_index, (seed_name, _seed_weight) in enumerate(top_seed_names[:2])
            }

        direct_results_by_seed = {}
        for seed_index, future in direct_seed_futures.items():
            try:
                direct_results_by_seed[seed_index] = future.result(timeout=8)
            except Exception:
                direct_results_by_seed[seed_index] = []

        semantic_results_by_seed = {}
        for seed_index, future in semantic_seed_futures.items():
            try:
                semantic_results_by_seed[seed_index] = future.result(timeout=8)
            except Exception:
                semantic_results_by_seed[seed_index] = []

        related_artist_futures = {}
        for seed_index, direct_results in direct_results_by_seed.items():
            if not direct_results:
                continue
            if surface == "search_results" and seed_index >= 3:
                continue
            if surface != "search_results" and seed_index >= 2:
                continue
            primary_artist_id = trim_text(direct_results[0].get("id"))
            if not primary_artist_id:
                continue
            related_artist_futures[seed_index] = self._executor().submit(
                self._server.build_artist_details_payload,
                primary_artist_id,
                enrich_related=(surface == "search_results"),
            )

        for seed_index, (_seed_name, seed_weight) in enumerate(top_seed_names):
            for index, artist in enumerate(direct_results_by_seed.get(seed_index) or []):
                add_artist_result(artist, seed_weight + max(2.0 - (index * 0.28), 0.7))
            for index, artist in enumerate(semantic_results_by_seed.get(seed_index) or []):
                add_artist_result(artist, max(seed_weight - 0.35 - (index * 0.18), 0.45))
            related_future = related_artist_futures.get(seed_index)
            if related_future is not None:
                try:
                    artist_payload = related_future.result(timeout=10)
                except Exception:
                    artist_payload = {}
                related_limit = 6 if surface == "search_results" else 4
                for index, related in enumerate((artist_payload.get("related_artists") or [])[:related_limit]):
                    add_artist_result(related, max(seed_weight - 0.55 - (index * 0.15), 0.4))

        if not artists and surface == "search_results":
            for query in query_seeds[:2]:
                for index, artist in enumerate(resolve_search_artists(self._server, query, 4)):
                    add_artist_result(artist, max(1.25 - (index * 0.18), 0.35))

        reference_vectors = _reference_vectors(
            anchor_tracks if surface == "search_results" else [],
            anchor_artist_names if surface == "search_results" else (profile.get("top_artists") or [])[:6],
            server=self._server,
        )
        artist_embeddings = self._server.recommendation_artist_embeddings(artists)
        ranked_artists = []
        for artist in artists:
            artist_key = self._server.recommendation_artist_embedding_key(artist)
            artist_vector = artist_embeddings.get(artist_key) or []
            seed_score = float(artist.get("score") or 0.0) * 0.45
            similarities = {
                "taste": self._server.cosine_similarity(artist_vector, profile_vectors.get("taste_vector") or []),
                "artist": self._server.cosine_similarity(artist_vector, profile_vectors.get("artist_vector") or []),
                "query": self._server.cosine_similarity(artist_vector, profile_vectors.get("query_vector") or []),
                "short": self._server.cosine_similarity(artist_vector, profile_vectors.get("short_term_vector") or []),
                "long": self._server.cosine_similarity(artist_vector, profile_vectors.get("long_term_vector") or []),
                "anchor_artist": self._server.cosine_similarity(artist_vector, reference_vectors.get("anchor_artist_vector") or []),
                "anchor_track": self._server.cosine_similarity(artist_vector, reference_vectors.get("anchor_track_vector") or []),
            }
            normalized_name = self._server.normalize_text(artist.get("name") or "")
            collaborative_score = float((collaborative.get("artist_scores") or {}).get(normalized_name) or 0.0)
            if surface == "search_results":
                ranking_score = (
                    (seed_score * 0.38)
                    + (similarities["anchor_artist"] * 6.2)
                    + (similarities["anchor_track"] * 4.4)
                    + (similarities["artist"] * 2.0)
                    + (similarities["query"] * 1.2)
                    + (similarities["taste"] * 0.9)
                    + (similarities["short"] * 0.6)
                    + (collaborative_score * 0.18)
                )
                ranking_score -= _anchor_penalty(
                    anchor_artist_names,
                    artist.get("name") or "",
                    server=self._server,
                )
            else:
                ranking_score = (
                    seed_score
                    + (similarities["taste"] * 5.1)
                    + (similarities["artist"] * 4.7)
                    + (similarities["query"] * 1.9)
                    + (similarities["short"] * 1.3)
                    + (similarities["long"] * 1.1)
                    + (max(similarities["anchor_artist"], similarities["anchor_track"]) * 0.75)
                )
                ranking_score += collaborative_score * 0.4
                if normalized_name in listened_artist_names:
                    ranking_score -= 0.9
            artist["score"] = round(ranking_score, 3)
            artist["ml_similarities"] = {name: round(value, 4) for name, value in similarities.items()}
            ranked_artists.append(artist)

        ranked_artists.sort(
            key=lambda item: (item.get("score", 0), len(self._server.normalize_text(item.get("name") or ""))),
            reverse=True,
        )
        model_version = build_recommendation_model_version(prefix="artist-neighborhood-v2", profile=profile)
        return {
            "status": "success",
            "artists": ranked_artists[: max(1, min(limit, 12))],
            "diagnostics": {
                "ranking_backend": "artist_neighborhood_v2",
                "request_ms": int((time.perf_counter() - request_started_at) * 1000),
                "model_version": model_version,
                "surface": surface,
                "seed_count": len(top_seed_names),
            },
            "model_version": model_version,
        }
