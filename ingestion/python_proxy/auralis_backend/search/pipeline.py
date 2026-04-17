from __future__ import annotations

from typing import Any, Dict, List

from ..domain.collaborative import track_scores as collaborative_track_scores
from ..domain.result_quality import (
    album_result_penalty,
    artist_result_penalty,
    track_result_penalty,
)
from ..recommend.feature_layer import (
    album_catalog_alignment,
    artist_catalog_alignment,
    build_catalog_feature_profile,
    candidate_catalog_alignment,
)
from .server_adapter import adapt_search_server
from .runtime import (
    semantic_search_lexical_score,
    semantic_search_vector_similarities,
    semantic_search_vectors,
)


def build_search_ranking_runtime(
    server: Any,
    req,
    profile: Dict[str, Any],
    retrieval_payload: Dict[str, Any],
) -> Dict[str, Any]:
    server = adapt_search_server(server)
    return {
        "query": server.trim_text(req.query),
        "search_vectors": semantic_search_vectors(req, profile, server=server),
        "catalog_profile": build_catalog_feature_profile(server, profile),
        "normalized_anchor_artists": retrieval_payload.get("normalized_anchor_artists")
        or set(),
        "collaborative_artist_scores": (
            (profile.get("collaborative") or {}).get("artist_scores") or {}
        ),
    }


def _finalize_ranked_tracks(
    server: Any,
    ranked: List[Dict[str, Any]],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    server = adapt_search_server(server)
    ranked.sort(
        key=lambda item: (
            float(item.get("score") or 0.0),
            item.get("title") or "",
        ),
        reverse=True,
    )
    results: List[Dict[str, Any]] = []
    artist_counts: Dict[str, int] = {}
    title_counts: Dict[str, int] = {}
    source_counts: Dict[str, int] = {}
    for track in ranked:
        artist_key = server.normalize_text(
            track.get("channel") or track.get("artist") or ""
        )
        if artist_key:
            artist_count = artist_counts.get(artist_key, 0)
            if artist_count >= 2 and len(results) + 1 < limit:
                continue
        title_key = server.normalize_text(track.get("title") or "")
        if title_key:
            title_count = title_counts.get(title_key, 0)
            if title_count >= 2 and len(results) + 1 < limit:
                continue
        source_name = server.trim_text(track.get("search_source"))
        if source_name:
            source_count = source_counts.get(source_name, 0)
            if source_count >= max(2, int(limit * 0.45)) and len(results) + 1 < limit:
                continue
        if artist_key:
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        if title_key:
            title_counts[title_key] = title_counts.get(title_key, 0) + 1
        if source_name:
            source_counts[source_name] = source_counts.get(source_name, 0) + 1
        results.append(track)
        if len(results) >= limit:
            break
    return results


def rank_track_candidates_fast_path(
    server: Any,
    req,
    retrieval_payload: Dict[str, Any],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    server = adapt_search_server(server)
    query = server.trim_text(req.query)
    track_candidates = retrieval_payload.get("track_candidates") or {}
    if not track_candidates:
        return []
    normalized_query = server.normalize_text(query)
    normalized_anchor_artists = retrieval_payload.get("normalized_anchor_artists") or set()
    ranked: List[Dict[str, Any]] = []
    for entry in track_candidates.values():
        payload = (entry or {}).get("payload")
        if not isinstance(payload, dict):
            continue
        track = server.normalize_track(payload)
        if track is None:
            continue
        source_scores = dict((entry or {}).get("source_scores") or {})
        source_names = sorted(
            source_name
            for source_name in source_scores.keys()
            if server.trim_text(source_name)
        )
        source_score = max(source_scores.values(), default=0.0)
        retrieval_votes = max(len(source_names), 1)
        title = track.get("title")
        artist = track.get("channel") or track.get("artist")
        album = track.get("album")
        lexical_score = semantic_search_lexical_score(query, title, artist, album, server=server)
        title_lexical = semantic_search_lexical_score(query, title, server=server)
        normalized_title = server.normalize_text(title or "")
        exact_title_match = 1.0 if normalized_title and normalized_title == normalized_query else 0.0
        anchor_artist_match = (
            1.0
            if server.normalize_text(artist or "") in normalized_anchor_artists
            else 0.0
        )
        ranking_score = (
            (float(source_score) * 1.2)
            + (float(retrieval_votes) * 0.45)
            + (float(lexical_score) * 6.0)
            + (float(title_lexical) * 5.4)
            + (float(exact_title_match) * 2.4)
            + (float(anchor_artist_match) * 0.65)
        )
        ranking_score -= track_result_penalty(
            server,
            track,
            query=query,
            normalized_anchor_artists=normalized_anchor_artists,
        )
        track["score"] = round(float(ranking_score), 3)
        track["search_source"] = source_names[0] if source_names else ""
        track["search_sources"] = source_names
        track["ranking_features"] = {
            "source_score": round(float(source_score), 4),
            "retrieval_votes": round(float(retrieval_votes), 4),
            "lexical": round(float(lexical_score), 4),
            "title_lexical": round(float(title_lexical), 4),
            "exact_title_match": round(float(exact_title_match), 4),
            "anchor_artist_match": round(float(anchor_artist_match), 4),
        }
        track["ml_similarities"] = {
            "lexical": round(float(lexical_score + title_lexical), 4),
            "anchor_artist_match": round(float(anchor_artist_match), 4),
        }
        ranked.append(track)
    return _finalize_ranked_tracks(server, ranked, limit=limit)


def rank_track_candidates(
    server: Any,
    req,
    profile: Dict[str, Any],
    retrieval_payload: Dict[str, Any],
    *,
    limit: int,
    ranking_runtime: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    server = adapt_search_server(server)
    runtime = ranking_runtime or build_search_ranking_runtime(
        server,
        req,
        profile,
        retrieval_payload,
    )
    query = runtime.get("query") or ""
    track_candidates = retrieval_payload.get("track_candidates") or {}
    if not track_candidates:
        return []

    candidate_items = [
        entry.get("payload")
        for entry in track_candidates.values()
        if isinstance(entry, dict) and isinstance(entry.get("payload"), dict)
    ]
    candidate_embeddings = server.recommendation_track_embeddings(candidate_items)
    search_vectors = runtime.get("search_vectors") or {}
    normalized_anchor_artists = runtime.get("normalized_anchor_artists") or set()
    catalog_profile = runtime.get("catalog_profile") or {}
    ranked: List[Dict[str, Any]] = []
    for entry in track_candidates.values():
        payload = (entry or {}).get("payload")
        if not isinstance(payload, dict):
            continue
        track = server.normalize_track(payload)
        if track is None:
            continue
        source_scores = dict((entry or {}).get("source_scores") or {})
        source_names = sorted(
            source_name
            for source_name in source_scores.keys()
            if server.trim_text(source_name)
        )
        source_score = max(source_scores.values(), default=0.0)
        retrieval_votes = max(len(source_names), 1)
        track_id = server.trim_text(track.get("id"))
        track_artist_key = server.normalize_text(
            track.get("channel") or track.get("artist") or ""
        )
        track_key = server.recommendation_track_embedding_key(track)
        track_vector = candidate_embeddings.get(track_key) or []
        similarities = semantic_search_vector_similarities(
            track_vector,
            search_vectors,
            profile,
            server=server,
        )
        collaborative_scores = collaborative_track_scores(server, track, profile)
        lexical_score = semantic_search_lexical_score(
            query,
            track.get("title"),
            track.get("channel"),
            track.get("album"),
            server=server,
        )
        title_lexical = semantic_search_lexical_score(
            query,
            track.get("title"),
            server=server,
        )
        popularity = min(float(collaborative_scores["neighbor"]) / 5.0, 1.0)
        if track_id in (profile.get("top_track_ids") or []):
            popularity = max(popularity, 1.0)
        anchor_artist_match = 1.0 if track_artist_key in normalized_anchor_artists else 0.0
        catalog_alignment = candidate_catalog_alignment(server, track, profile)
        overexposed_artist_penalty = (
            1.0
            if track_artist_key
            and track_artist_key in set(catalog_profile.get("dominant_artist_keys") or set())
            else 0.0
        )

        ranking_features = {
            "source_score": float(source_score),
            "retrieval_votes": float(retrieval_votes),
            "lexical": float(lexical_score),
            "title_lexical": float(title_lexical),
            "query_similarity": float(similarities["query"]),
            "semantic_query_similarity": float(similarities["semantic_query"]),
            "context_similarity": float(similarities["context"]),
            "taste_similarity": float(similarities["taste"]),
            "artist_similarity": float(similarities["artist"]),
            "short_similarity": float(similarities["short"]),
            "long_similarity": float(similarities["long"]),
            "collab_latent": float(collaborative_scores["latent"]),
            "collab_neighbor": min(float(collaborative_scores["neighbor"]), 5.0),
            "collab_artist": min(float(collaborative_scores["artist"]), 6.0),
            "anchor_artist_match": float(anchor_artist_match),
            "popularity": float(popularity),
            "genre_fit": float(catalog_alignment.get("genre_affinity") or 0.0),
            "subgenre_fit": float(catalog_alignment.get("subgenre_affinity") or 0.0),
            "scene_fit": float(catalog_alignment.get("scene_affinity") or 0.0),
            "peer_scene_bonus": float(catalog_alignment.get("peer_scene_bonus") or 0.0),
            "era_fit": float(catalog_alignment.get("era_affinity") or 0.0),
            "adjacent_era_fit": float(catalog_alignment.get("adjacent_era_affinity") or 0.0),
            "language_fit": float(catalog_alignment.get("language_affinity") or 0.0),
            "script_fit": float(catalog_alignment.get("script_affinity") or 0.0),
            "track_type_fit": float(catalog_alignment.get("type_affinity") or 0.0),
            "popularity_taste_fit": float(catalog_alignment.get("popularity_taste_fit") or 0.0),
            "novelty_tolerance_fit": float(catalog_alignment.get("novelty_tolerance_fit") or 0.0),
            "negative_feedback_penalty": float(catalog_alignment.get("negative_feedback_penalty") or 0.0),
            "same_title_ambiguity_penalty": float(catalog_alignment.get("same_title_ambiguity_penalty") or 0.0),
            "overexposed_artist_penalty": float(overexposed_artist_penalty),
        }
        ranking_score = server.ranking_score_features(
            model_key="search_track_reranker_v2",
            defaults=server.SEARCH_TRACK_DEFAULT_WEIGHTS,
            features=ranking_features,
        )
        ranking_score -= track_result_penalty(
            server,
            track,
            query=query,
            normalized_anchor_artists=normalized_anchor_artists,
        )
        track["score"] = round(float(ranking_score), 3)
        track["search_source"] = source_names[0] if source_names else ""
        track["search_sources"] = source_names
        track["ranking_features"] = {
            feature_key: round(float(feature_value), 4)
            for feature_key, feature_value in ranking_features.items()
        }
        track["item_feature_summary"] = dict(catalog_alignment.get("item_feature_summary") or {})
        track["ml_similarities"] = {
            "query": round(similarities["query"], 4),
            "semantic_query": round(similarities["semantic_query"], 4),
            "context": round(similarities["context"], 4),
            "taste": round(similarities["taste"], 4),
            "artist": round(similarities["artist"], 4),
            "short": round(similarities["short"], 4),
            "long": round(similarities["long"], 4),
            "lexical": round(lexical_score + title_lexical, 4),
            "collab_latent": round(collaborative_scores["latent"], 4),
            "collab_neighbor": round(collaborative_scores["neighbor"], 4),
            "collab_artist": round(collaborative_scores["artist"], 4),
            "scene_fit": round(float(catalog_alignment.get("scene_affinity") or 0.0), 4),
            "genre_fit": round(float(catalog_alignment.get("genre_affinity") or 0.0), 4),
            "era_fit": round(float(catalog_alignment.get("era_affinity") or 0.0), 4),
            "language_fit": round(float(catalog_alignment.get("language_affinity") or 0.0), 4),
            "negative_feedback_penalty": round(float(catalog_alignment.get("negative_feedback_penalty") or 0.0), 4),
        }
        ranked.append(track)

    return _finalize_ranked_tracks(server, ranked, limit=limit)


def rank_artist_candidates(
    server: Any,
    req,
    profile: Dict[str, Any],
    retrieval_payload: Dict[str, Any],
    *,
    limit: int,
    ranking_runtime: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    server = adapt_search_server(server)
    runtime = ranking_runtime or build_search_ranking_runtime(
        server,
        req,
        profile,
        retrieval_payload,
    )
    query = runtime.get("query") or ""
    artist_candidates = retrieval_payload.get("artist_candidates") or {}
    if not artist_candidates:
        return []
    candidate_items = [
        entry.get("payload")
        for entry in artist_candidates.values()
        if isinstance(entry, dict) and isinstance(entry.get("payload"), dict)
    ]
    artist_embeddings = server.recommendation_artist_embeddings(candidate_items)
    search_vectors = runtime.get("search_vectors") or {}
    collaborative_artist_scores = runtime.get("collaborative_artist_scores") or {}
    catalog_profile = runtime.get("catalog_profile") or {}
    ranked: List[Dict[str, Any]] = []
    for entry in artist_candidates.values():
        artist = dict((entry or {}).get("payload") or {})
        artist_name = server.trim_text(artist.get("name"))
        if not artist_name:
            continue
        source_scores = dict((entry or {}).get("source_scores") or {})
        source_names = sorted(
            source_name
            for source_name in source_scores.keys()
            if server.trim_text(source_name)
        )
        source_score = max(source_scores.values(), default=0.0)
        retrieval_votes = max(len(source_names), 1)
        artist_key = server.recommendation_artist_embedding_key(artist)
        artist_vector = artist_embeddings.get(artist_key) or []
        similarities = semantic_search_vector_similarities(
            artist_vector,
            search_vectors,
            profile,
            server=server,
        )
        lexical_score = semantic_search_lexical_score(
            query,
            artist.get("name"),
            artist.get("description"),
            server=server,
        )
        catalog_alignment = artist_catalog_alignment(server, artist, profile)
        overexposed_artist_penalty = (
            1.0
            if server.normalize_text(artist_name or "") in set(catalog_profile.get("dominant_artist_keys") or set())
            else 0.0
        )
        collaborative_artist_score = float(
            collaborative_artist_scores.get(
                server.normalize_text(artist_name)
            )
            or 0.0
        )
        ranking_features = {
            "source_score": float(source_score),
            "retrieval_votes": float(retrieval_votes),
            "lexical": float(lexical_score),
            "query_similarity": float(similarities["query"]),
            "semantic_query_similarity": float(similarities["semantic_query"]),
            "context_similarity": float(similarities["context"]),
            "taste_similarity": float(similarities["taste"]),
            "artist_similarity": float(similarities["artist"]),
            "anchor_artist_similarity": float(similarities["artist"]),
            "anchor_track_similarity": float(similarities["context"]),
            "collab_artist": min(collaborative_artist_score, 6.0),
            "genre_fit": float(catalog_alignment.get("genre_affinity") or 0.0),
            "subgenre_fit": float(catalog_alignment.get("subgenre_affinity") or 0.0),
            "scene_fit": float(catalog_alignment.get("scene_affinity") or 0.0),
            "peer_scene_bonus": float(catalog_alignment.get("peer_scene_bonus") or 0.0),
            "era_fit": float(catalog_alignment.get("era_affinity") or 0.0),
            "adjacent_era_fit": float(catalog_alignment.get("adjacent_era_affinity") or 0.0),
            "language_fit": float(catalog_alignment.get("language_affinity") or 0.0),
            "script_fit": float(catalog_alignment.get("script_affinity") or 0.0),
            "negative_feedback_penalty": float(catalog_alignment.get("negative_feedback_penalty") or 0.0),
            "overexposed_artist_penalty": float(overexposed_artist_penalty),
        }
        ranking_score = server.ranking_score_features(
            model_key="search_artist_reranker_v2",
            defaults=server.SEARCH_ARTIST_DEFAULT_WEIGHTS,
            features=ranking_features,
        )
        ranking_score -= artist_result_penalty(server, artist, query=query)
        artist["score"] = round(float(ranking_score), 3)
        artist["search_source"] = source_names[0] if source_names else ""
        artist["search_sources"] = source_names
        artist["ranking_features"] = {
            feature_key: round(float(feature_value), 4)
            for feature_key, feature_value in ranking_features.items()
        }
        artist["item_feature_summary"] = dict(catalog_alignment.get("item_feature_summary") or {})
        artist["ml_similarities"] = {
            "query": round(similarities["query"], 4),
            "semantic_query": round(similarities["semantic_query"], 4),
            "context": round(similarities["context"], 4),
            "taste": round(similarities["taste"], 4),
            "artist": round(similarities["artist"], 4),
            "collab_artist": round(collaborative_artist_score, 4),
            "lexical": round(lexical_score, 4),
            "scene_fit": round(float(catalog_alignment.get("scene_affinity") or 0.0), 4),
            "genre_fit": round(float(catalog_alignment.get("genre_affinity") or 0.0), 4),
            "negative_feedback_penalty": round(float(catalog_alignment.get("negative_feedback_penalty") or 0.0), 4),
        }
        ranked.append(artist)
    ranked.sort(
        key=lambda item: (
            float(item.get("score") or 0.0),
            item.get("name") or "",
        ),
        reverse=True,
    )
    results: List[Dict[str, Any]] = []
    for artist in ranked:
        if any(
            server.normalize_text(existing.get("name") or "")
            == server.normalize_text(artist.get("name") or "")
            for existing in results
        ):
            continue
        results.append(artist)
        if len(results) >= limit:
            break
    return results


def rank_album_candidates(
    server: Any,
    req,
    profile: Dict[str, Any],
    retrieval_payload: Dict[str, Any],
    *,
    limit: int,
    ranking_runtime: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    server = adapt_search_server(server)
    runtime = ranking_runtime or build_search_ranking_runtime(
        server,
        req,
        profile,
        retrieval_payload,
    )
    query = runtime.get("query") or ""
    album_candidates = retrieval_payload.get("album_candidates") or {}
    if not album_candidates:
        return []
    candidate_items = [
        entry.get("payload")
        for entry in album_candidates.values()
        if isinstance(entry, dict) and isinstance(entry.get("payload"), dict)
    ]
    album_embeddings = server.recommendation_album_embeddings(candidate_items)
    search_vectors = runtime.get("search_vectors") or {}
    collaborative_artist_scores = runtime.get("collaborative_artist_scores") or {}
    catalog_profile = runtime.get("catalog_profile") or {}
    ranked: List[Dict[str, Any]] = []
    for entry in album_candidates.values():
        album = dict((entry or {}).get("payload") or {})
        album_title = server.trim_text(album.get("title"))
        if not album_title:
            continue
        source_scores = dict((entry or {}).get("source_scores") or {})
        source_names = sorted(
            source_name
            for source_name in source_scores.keys()
            if server.trim_text(source_name)
        )
        source_score = max(source_scores.values(), default=0.0)
        retrieval_votes = max(len(source_names), 1)
        album_key = server.recommendation_album_embedding_key(album)
        album_vector = album_embeddings.get(album_key) or []
        similarities = semantic_search_vector_similarities(
            album_vector,
            search_vectors,
            profile,
            server=server,
        )
        lexical_score = semantic_search_lexical_score(
            query,
            album.get("title"),
            album.get("artist"),
            server=server,
        )
        catalog_alignment = album_catalog_alignment(server, album, profile)
        overexposed_artist_penalty = (
            1.0
            if server.normalize_text(album.get("artist") or "") in set(catalog_profile.get("dominant_artist_keys") or set())
            else 0.0
        )
        collaborative_artist_score = float(
            collaborative_artist_scores.get(
                server.normalize_text(album.get("artist") or "")
            )
            or 0.0
        )
        ranking_features = {
            "source_score": float(source_score),
            "retrieval_votes": float(retrieval_votes),
            "lexical": float(lexical_score),
            "query_similarity": float(similarities["query"]),
            "semantic_query_similarity": float(similarities["semantic_query"]),
            "context_similarity": float(similarities["context"]),
            "taste_similarity": float(similarities["taste"]),
            "artist_similarity": float(similarities["artist"]),
            "collab_artist": min(collaborative_artist_score, 6.0),
            "genre_fit": float(catalog_alignment.get("genre_affinity") or 0.0),
            "subgenre_fit": float(catalog_alignment.get("subgenre_affinity") or 0.0),
            "scene_fit": float(catalog_alignment.get("scene_affinity") or 0.0),
            "peer_scene_bonus": float(catalog_alignment.get("peer_scene_bonus") or 0.0),
            "era_fit": float(catalog_alignment.get("era_affinity") or 0.0),
            "adjacent_era_fit": float(catalog_alignment.get("adjacent_era_affinity") or 0.0),
            "language_fit": float(catalog_alignment.get("language_affinity") or 0.0),
            "script_fit": float(catalog_alignment.get("script_affinity") or 0.0),
            "popularity_taste_fit": float(catalog_alignment.get("popularity_taste_fit") or 0.0),
            "negative_feedback_penalty": float(catalog_alignment.get("negative_feedback_penalty") or 0.0),
            "same_title_ambiguity_penalty": float(catalog_alignment.get("same_title_ambiguity_penalty") or 0.0),
            "overexposed_artist_penalty": float(overexposed_artist_penalty),
        }
        ranking_score = server.ranking_score_features(
            model_key="search_album_reranker_v2",
            defaults=server.SEARCH_ALBUM_DEFAULT_WEIGHTS,
            features=ranking_features,
        )
        ranking_score -= album_result_penalty(server, album, query=query)
        album["score"] = round(float(ranking_score), 3)
        album["search_source"] = source_names[0] if source_names else ""
        album["search_sources"] = source_names
        album["ranking_features"] = {
            feature_key: round(float(feature_value), 4)
            for feature_key, feature_value in ranking_features.items()
        }
        album["item_feature_summary"] = dict(catalog_alignment.get("item_feature_summary") or {})
        album["ml_similarities"] = {
            "query": round(similarities["query"], 4),
            "semantic_query": round(similarities["semantic_query"], 4),
            "context": round(similarities["context"], 4),
            "taste": round(similarities["taste"], 4),
            "artist": round(similarities["artist"], 4),
            "collab_artist": round(collaborative_artist_score, 4),
            "lexical": round(lexical_score, 4),
            "scene_fit": round(float(catalog_alignment.get("scene_affinity") or 0.0), 4),
            "era_fit": round(float(catalog_alignment.get("era_affinity") or 0.0), 4),
            "same_title_ambiguity_penalty": round(float(catalog_alignment.get("same_title_ambiguity_penalty") or 0.0), 4),
            "negative_feedback_penalty": round(float(catalog_alignment.get("negative_feedback_penalty") or 0.0), 4),
        }
        ranked.append(album)
    ranked.sort(
        key=lambda item: (
            float(item.get("score") or 0.0),
            item.get("title") or "",
        ),
        reverse=True,
    )
    results: List[Dict[str, Any]] = []
    artist_counts: Dict[str, int] = {}
    for album in ranked:
        artist_key = server.normalize_text(album.get("artist") or "")
        if artist_key:
            artist_count = artist_counts.get(artist_key, 0)
            if artist_count >= 2 and len(results) + 1 < limit:
                continue
            artist_counts[artist_key] = artist_count + 1
        results.append(album)
        if len(results) >= limit:
            break
    return results


def summarize_ranked_results(
    server: Any,
    *,
    tracks: List[Dict[str, Any]],
    artists: List[Dict[str, Any]],
    albums: List[Dict[str, Any]],
) -> Dict[str, Any]:
    server = adapt_search_server(server)
    track_sources: Dict[str, int] = {}
    artist_sources: Dict[str, int] = {}
    album_sources: Dict[str, int] = {}
    unique_track_artists = {
        server.normalize_text(track.get("channel") or track.get("artist") or "")
        for track in tracks or []
        if server.normalize_text(track.get("channel") or track.get("artist") or "")
    }
    for track in tracks or []:
        for source_name in track.get("search_sources") or []:
            track_sources[source_name] = track_sources.get(source_name, 0) + 1
    for artist in artists or []:
        for source_name in artist.get("search_sources") or []:
            artist_sources[source_name] = artist_sources.get(source_name, 0) + 1
    for album in albums or []:
        for source_name in album.get("search_sources") or []:
            album_sources[source_name] = album_sources.get(source_name, 0) + 1
    return {
        "track_source_counts": track_sources,
        "artist_source_counts": artist_sources,
        "album_source_counts": album_sources,
        "unique_track_artists": len(unique_track_artists),
        "tracks_ranked": len(tracks or []),
        "artists_ranked": len(artists or []),
        "albums_ranked": len(albums or []),
    }
