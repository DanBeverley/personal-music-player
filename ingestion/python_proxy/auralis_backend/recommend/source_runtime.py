from __future__ import annotations

import time
from typing import Any, Dict, Optional

from ..domain.candidate_expansion import (
    album_candidates_for_track as _shared_album_candidates_for_track,
    anchor_query as _shared_anchor_query,
    candidate_sources_for_track as _shared_candidate_sources_for_track,
)
from ..runtime_context import resolve_server
from ..search.runtime import search_artist_seed_tracks  # compatibility import for tests
from ..search.upstream_runtime import search_artists_direct
from .feature_layer import album_catalog_alignment, build_catalog_feature_profile


def _server(server: Any | None = None):
    return resolve_server(server)


def _recommendation_quiet_base_query(profile, *, server: Any | None = None) -> str:
    server = _server(server)
    top_artists = server._recommendation_unique_strings(
        [
            *(profile.get("top_artists") or []),
            *(profile.get("artist_hints") or []),
            *(profile.get("listened_artists") or []),
        ],
        6,
    )
    if top_artists:
        return top_artists[0]
    top_albums = server._recommendation_unique_strings(
        [
            *(profile.get("top_albums") or []),
            *(profile.get("album_hints") or []),
        ],
        4,
    )
    if top_albums:
        return top_albums[0]
    return ""


def _recommendation_anchor_query(
    track: Optional[Dict[str, Any]],
    *,
    include_album: bool = False,
    server: Any | None = None,
) -> str:
    return _shared_anchor_query(track, include_album=include_album)


def _recommendation_album_candidates_for_track(
    track: Optional[Dict[str, Any]],
    *,
    limit: int = 2,
    server: Any | None = None,
):
    return _shared_album_candidates_for_track(
        track,
        limit=limit,
        include_search=True,
        server=_server(server),
    )


def _recommendation_candidate_sources_for_track(
    track: Optional[Dict[str, Any]],
    *,
    server: Any | None = None,
):
    server = _server(server)
    if not isinstance(track, dict):
        return []

    artist_name = server._recommendation_trim_text(
        track.get("channel") or track.get("author") or track.get("artist")
    )
    source_results = {
        "similar": [],
        "collaborative": [],
        "artist_seed": [],
        "album_context": [],
        "fallback_context": [],
    }
    for source_name, source_tracks, _base_score in _shared_candidate_sources_for_track(
        track,
        server=server,
    ):
        if source_name in source_results:
            source_results[source_name] = list(source_tracks or [])

    if not (
        source_results["similar"]
        or source_results["collaborative"]
        or source_results["artist_seed"]
    ):
        fallback_tracks = _recommendation_anchor_fallback_tracks(track, limit=16)
        if artist_name:
            normalized_artist = server._normalize_text(artist_name)
            filtered = [
                fallback_track
                for fallback_track in fallback_tracks
                if server._normalize_text(
                    fallback_track.get("channel")
                    or fallback_track.get("artist")
                    or ""
                ) == normalized_artist
            ]
            source_results["fallback_context"] = filtered[:10] or fallback_tracks[:8]
        else:
            source_results["fallback_context"] = fallback_tracks[:8]

    return [
        ("similar", source_results["similar"], 4.8),
        ("collaborative", source_results["collaborative"], 4.6),
        ("artist_seed", source_results["artist_seed"], 3.7),
        ("album_context", source_results["album_context"], 3.5),
        ("fallback_context", source_results["fallback_context"], 2.4),
    ]


def _recommendation_anchor_fallback_tracks(
    track: Optional[Dict[str, Any]],
    *,
    limit: int,
    server: Any | None = None,
):
    server = _server(server)
    if not isinstance(track, dict):
        return []

    artist_name = server._recommendation_trim_text(
        track.get("channel") or track.get("author") or track.get("artist")
    )
    title = server._recommendation_trim_text(track.get("title"))
    album = server._recommendation_trim_text(track.get("album"))
    seen_keys = set()
    ranked_tracks = []

    def add_track(raw_track: Optional[Dict[str, Any]], base_score: float) -> None:
        if not isinstance(raw_track, dict):
            return
        track_id = server._recommendation_trim_text(raw_track.get("id"))
        key = track_id or "|".join(
            [
                server._normalize_text(raw_track.get("title") or ""),
                server._normalize_text(
                    raw_track.get("channel") or raw_track.get("author") or raw_track.get("artist") or ""
                ),
                server._normalize_text(raw_track.get("album") or ""),
            ]
        )
        if not key or key in seen_keys:
            return
        seen_keys.add(key)
        ranked_tracks.append((base_score, raw_track))

    queries = []
    if title and artist_name:
        queries.append(f"{title} {artist_name}")
    if artist_name:
        queries.append(artist_name)
    if title and album:
        queries.append(f"{title} {album}")

    for index, query in enumerate(server._recommendation_unique_strings(queries, 3)):
        try:
            results = server._assistant_tool_search_tracks(query, max(limit * 2, 8))
        except Exception:
            results = []
        for result in results or []:
            add_track(result, max(2.4 - (index * 0.15), 1.9))
        if len(ranked_tracks) >= max(limit * 2, 12):
            break

    if len(ranked_tracks) < max(4, limit // 2):
        for base_index, fallback_track in enumerate(
            [item for item, _score in server._fallback_home_candidates(limit + 8)]
        ):
            add_track(fallback_track, max(1.6 - (base_index * 0.02), 1.1))
            if len(ranked_tracks) >= max(limit * 2, 12):
                break

    ranked_tracks.sort(key=lambda item: item[0], reverse=True)
    return [track_item for _score, track_item in ranked_tracks[: max(1, limit)]]


def _recommendation_home_fallback_tracks(
    profile,
    *,
    limit: int,
    allow_catalog_fallback: bool = True,
    server: Any | None = None,
):
    server = _server(server)
    profile = dict(profile or {})
    candidate_tracks = []

    candidate_tracks.extend(
        server._recommendation_unique_snapshot_tracks(
            [
                *(profile.get("last_played_tracks") or []),
                *(profile.get("recent_track_snapshots") or []),
                *(profile.get("top_track_snapshots") or []),
            ],
            max(limit * 2, limit + 8),
        )
    )

    collaborative_ids = list(
        ((profile.get("collaborative") or {}).get("candidate_track_ids") or [])
    )[: max(limit, 12)]
    if collaborative_ids:
        candidate_tracks.extend(
            server._recommendation_fetch_tracks_for_ids(
                collaborative_ids,
                limit=max(limit, 12),
            )
        )

    library_track_ids = list(
        profile.get("offline_track_ids") or profile.get("library_track_ids") or []
    )[: max(limit, 12)]
    if library_track_ids:
        candidate_tracks.extend(
            server._recommendation_fetch_tracks_for_ids(
                library_track_ids,
                limit=max(limit, 12),
            )
        )

    deduped_candidates = server._recommendation_unique_snapshot_tracks(
        candidate_tracks,
        max(limit * 3, limit + 10),
    )

    if allow_catalog_fallback and len(deduped_candidates) < max(6, limit // 2):
        deduped_candidates.extend(
            [item for item, _score in server._fallback_home_candidates(limit + 8)]
        )
        deduped_candidates = server._recommendation_unique_snapshot_tracks(
            deduped_candidates,
            max(limit * 3, limit + 10),
        )

    return _recommendation_taste_filtered_tracks(
        deduped_candidates,
        profile,
        limit=limit,
        server=server,
    )


def _recommendation_recommended_albums_row(profile, *, server: Any | None = None):
    server = _server(server)
    started_at = time.perf_counter()
    albums = []
    seen = set()
    candidate_cache_entries = []
    cached_candidate_count = 0
    source_counts = {
        "cache": 0,
        "top_album_hints": 0,
        "artist_discography": 0,
        "artist_album_search": 0,
        "snapshot_albums": 0,
    }
    fast_ready_threshold = 8
    catalog_profile = build_catalog_feature_profile(server, profile)
    affinity_artists = {
        server._normalize_text(name)
        for name in [
            *(profile.get("top_artists") or []),
            *(profile.get("artist_hints") or []),
            *(profile.get("listened_artists") or []),
        ]
        if server._normalize_text(name)
    }
    album_seed_artists: Dict[str, set[str]] = {}
    for track in [
        *(profile.get("last_played_tracks") or []),
        *(profile.get("top_track_snapshots") or []),
        *(profile.get("recent_track_snapshots") or []),
    ]:
        if not isinstance(track, dict):
            continue
        album_title = server._normalize_text(track.get("album") or "")
        artist_name = server._normalize_text(
            track.get("channel") or track.get("artist") or track.get("author") or ""
        )
        if not album_title or not artist_name:
            continue
        album_seed_artists.setdefault(album_title, set()).add(artist_name)

    def add_album(
        raw_album,
        base_score: float,
        *,
        required_artist_keys: Optional[set[str]] = None,
        source_name: str = "",
    ):
        if not isinstance(raw_album, dict):
            return False
        album_id = server._recommendation_trim_text(raw_album.get("id"))
        title = server._recommendation_trim_text(raw_album.get("title"))
        artist = server._recommendation_trim_text(raw_album.get("artist"))
        artist_key = server._normalize_text(artist)
        title_key = server._normalize_text(title)
        if required_artist_keys and artist_key not in required_artist_keys:
            return False
        if (
            title_key in album_seed_artists
            and artist_key
            and artist_key not in album_seed_artists[title_key]
            and artist_key not in affinity_artists
        ):
            return False
        key = album_id or f"{server._normalize_text(title)}|{server._normalize_text(artist)}"
        if not key or key in seen:
            return False
        seen.add(key)
        normalized_album = {
            "id": album_id or None,
            "title": title or "Unknown Album",
            "artist": artist or "Unknown Artist",
            "thumbnail": raw_album.get("thumbnail"),
            "year": raw_album.get("year") or "",
            "track_count": raw_album.get("track_count") or raw_album.get("trackCount") or 0,
            "generator_score": round(base_score, 3),
            "source_name": source_name or "",
        }
        candidate_cache_entries.append(dict(normalized_album))
        albums.append(
            dict(normalized_album)
        )
        if source_name:
            source_counts[source_name] = int(source_counts.get(source_name) or 0) + 1
        return True

    cached_candidates = list(
        (dict(profile).get("recommended_album_candidate_cache") or [])
    )
    for cached_candidate in cached_candidates:
        if not isinstance(cached_candidate, dict):
            continue
        if add_album(
            cached_candidate,
            float(cached_candidate.get("generator_score") or 0.0),
            source_name="cache",
        ):
            cached_candidate_count += 1

    if len(albums) < fast_ready_threshold:
        for index, album_hint in enumerate(profile.get("top_albums") or []):
            normalized_hint = server._normalize_text(album_hint)
            required_artist_keys = set(album_seed_artists.get(normalized_hint) or set()) or None
            for offset, album in enumerate(server._assistant_tool_search_albums(album_hint, 5)):
                add_album(
                    album,
                    4.2 - (index * 0.28) - (offset * 0.16),
                    required_artist_keys=required_artist_keys,
                    source_name="top_album_hints",
                )
                if len(albums) >= 18:
                    break
            if len(albums) >= 18:
                break

    if len(albums) < fast_ready_threshold:
        for index, artist_hint in enumerate(profile.get("top_artists") or []):
            direct_artists = search_artists_direct(server, artist_hint, 1)
            if direct_artists:
                artist_id = server._recommendation_trim_text(direct_artists[0].get("id"))
                if artist_id:
                    try:
                        artist_payload = server._build_artist_details_payload(artist_id)
                    except Exception:
                        artist_payload = {}
                    for offset, album in enumerate(artist_payload.get("albums") or []):
                        add_album(
                            album,
                            4.0 - (index * 0.22) - (offset * 0.18),
                            required_artist_keys={server._normalize_text(artist_hint)},
                            source_name="artist_discography",
                        )
                        if len(albums) >= 18:
                            break
            if len(albums) < fast_ready_threshold:
                for offset, album in enumerate(server._assistant_tool_search_albums(f"{artist_hint} album", 4)):
                    add_album(
                        album,
                        3.6 - (index * 0.18) - (offset * 0.14),
                        required_artist_keys={server._normalize_text(artist_hint)},
                        source_name="artist_album_search",
                    )
                    if len(albums) >= 18:
                        break
            if len(albums) >= 18:
                break

    if len(albums) < fast_ready_threshold:
        snapshot_tracks = [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("top_track_snapshots") or []),
            *(profile.get("recent_track_snapshots") or []),
        ]
        for index, track in enumerate(snapshot_tracks):
            album_title = server._recommendation_trim_text(track.get("album"))
            if not album_title:
                continue
            add_album(
                {
                    "id": track.get("album_id"),
                    "title": album_title,
                    "artist": track.get("channel"),
                    "thumbnail": track.get("thumbnail"),
                },
                3.2 - (index * 0.12),
                required_artist_keys={
                    server._normalize_text(
                        track.get("channel") or track.get("artist") or track.get("author") or ""
                    )
                },
                source_name="snapshot_albums",
            )
            if len(albums) >= 18:
                break

    if len(albums) < 1:
        return None

    album_embeddings = server._recommendation_album_embeddings(albums)
    vectors = profile.get("vectors") or {}
    top_album_names = {
        server._normalize_text(name)
        for name in (profile.get("top_albums") or [])
        if server._normalize_text(name)
    }
    top_artist_names = {
        server._normalize_text(name)
        for name in (profile.get("top_artists") or [])
        if server._normalize_text(name)
    }
    for album in albums:
        album_key = server._recommendation_album_embedding_key(album)
        album_vector = album_embeddings.get(album_key) or []
        catalog_alignment = album_catalog_alignment(server, album, profile)
        overexposed_artist_penalty = (
            1.0
            if server._normalize_text(album.get("artist") or "")
            in set(catalog_profile.get("dominant_artist_keys") or set())
            else 0.0
        )
        similarities = {
            "taste": server._assistant_cosine_similarity(
                album_vector,
                vectors.get("taste_vector") or [],
            ),
            "artist": server._assistant_cosine_similarity(
                album_vector,
                vectors.get("artist_vector") or [],
            ),
            "long": server._assistant_cosine_similarity(
                album_vector,
                vectors.get("long_term_vector") or [],
            ),
        }
        ranking_score = (
            (float(album.get("generator_score") or 0.0) * 0.45)
            + (similarities["taste"] * 4.8)
            + (similarities["artist"] * 3.6)
            + (similarities["long"] * 1.4)
            + (float(catalog_alignment.get("scene_affinity") or 0.0) * 1.65)
            + (float(catalog_alignment.get("genre_affinity") or 0.0) * 1.15)
            + (float(catalog_alignment.get("subgenre_affinity") or 0.0) * 0.6)
            + (float(catalog_alignment.get("era_affinity") or 0.0) * 0.8)
            + (float(catalog_alignment.get("adjacent_era_affinity") or 0.0) * 0.35)
            + (float(catalog_alignment.get("language_affinity") or 0.0) * 0.4)
            + (float(catalog_alignment.get("popularity_taste_fit") or 0.0) * 0.45)
            - (float(catalog_alignment.get("negative_feedback_penalty") or 0.0) * 2.2)
            - (float(catalog_alignment.get("same_title_ambiguity_penalty") or 0.0) * 2.1)
            - (float(overexposed_artist_penalty) * 0.75)
        )
        if server._normalize_text(album.get("title") or "") in top_album_names:
            ranking_score += 0.8
        if server._normalize_text(album.get("artist") or "") in top_artist_names:
            ranking_score += 0.6
        album["generator_score"] = round(ranking_score, 3)
        album["item_feature_summary"] = dict(catalog_alignment.get("item_feature_summary") or {})
        album["ml_similarities"] = {
            name: round(value, 4)
            for name, value in similarities.items()
        }
        album["ml_similarities"].update(
            {
                "scene_fit": round(float(catalog_alignment.get("scene_affinity") or 0.0), 4),
                "genre_fit": round(float(catalog_alignment.get("genre_affinity") or 0.0), 4),
                "era_fit": round(float(catalog_alignment.get("era_affinity") or 0.0), 4),
                "language_fit": round(float(catalog_alignment.get("language_affinity") or 0.0), 4),
                "negative_feedback_penalty": round(float(catalog_alignment.get("negative_feedback_penalty") or 0.0), 4),
                "same_title_ambiguity_penalty": round(float(catalog_alignment.get("same_title_ambiguity_penalty") or 0.0), 4),
            }
        )

    albums.sort(key=lambda item: item.get("generator_score", 0), reverse=True)
    return {
        "title": "Recommended albums",
        "kind": "recommended_albums",
        "item_type": "album",
        "items": albums[:18],
        "meta": {
            "build_ms": int((time.perf_counter() - started_at) * 1000),
            "source_counts": source_counts,
            "fast_ready_threshold": fast_ready_threshold,
            "ready_count": len(albums[:18]),
            "cached_candidate_count": cached_candidate_count,
        },
        "_server_refinement_cache": {
            "recommended_album_candidate_cache": candidate_cache_entries[:32],
        },
    }


def _recommendation_taste_filtered_tracks(
    tracks,
    profile,
    *,
    limit: int,
    server: Any | None = None,
):
    server = _server(server)
    ranked = []
    artist_hints = {
        server._normalize_text(item)
        for item in server._recommendation_unique_strings(
            [
                *(profile.get("top_artists") or []),
                *(profile.get("artist_hints") or []),
                *(profile.get("listened_artists") or []),
            ],
            14,
        )
        if server._normalize_text(item)
    }
    album_hints = {
        server._normalize_text(item)
        for item in server._recommendation_unique_strings(
            [
                *(profile.get("top_albums") or []),
                *(profile.get("album_hints") or []),
            ],
            8,
        )
        if server._normalize_text(item)
    }
    title_hints = {
        server._normalize_text(item.get("title"))
        for item in server._recommendation_unique_snapshot_tracks(
            [
                *(profile.get("top_track_snapshots") or []),
                *(profile.get("recent_track_snapshots") or []),
                *(profile.get("last_played_tracks") or []),
            ],
            10,
        )
        if server._normalize_text(item.get("title"))
    }
    for index, track in enumerate(tracks or []):
        if not isinstance(track, dict):
            continue
        artist_key = server._normalize_text(
            track.get("channel") or track.get("author") or track.get("artist") or ""
        )
        title_key = server._normalize_text(track.get("title") or "")
        album_key = server._normalize_text(track.get("album") or "")
        taste_score = 0.0
        if artist_key and artist_key in artist_hints:
            taste_score += 2.0
        if album_key and album_key in album_hints:
            taste_score += 1.1
        if title_key and title_key in title_hints:
            taste_score += 0.6
        taste_score += max(0.0, 0.25 - (index * 0.015))
        ranked.append((taste_score, track))
    ranked.sort(key=lambda item: item[0], reverse=True)
    selected = [track for _score, track in ranked[: max(1, limit)]]
    if selected:
        return selected
    return list(tracks or [])[: max(1, limit)]
