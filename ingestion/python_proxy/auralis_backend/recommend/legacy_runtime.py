from __future__ import annotations

import time
from typing import Any, Dict, Optional

from ..legacy import get_server
from ..search.runtime import search_artist_seed_tracks


def _server():
    return get_server()


def _recommendation_quiet_base_query(profile) -> str:
    server = _server()
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
) -> str:
    server = _server()
    if not isinstance(track, dict):
        return ""
    parts = [
        server._recommendation_trim_text(track.get("title")),
        server._recommendation_trim_text(
            track.get("channel") or track.get("author") or track.get("artist")
        ),
    ]
    if include_album:
        parts.append(server._recommendation_trim_text(track.get("album")))
    return " ".join([part for part in parts if part]).strip()


def _recommendation_album_candidates_for_track(
    track: Optional[Dict[str, Any]],
    *,
    limit: int = 2,
):
    server = _server()
    if not isinstance(track, dict):
        return []

    albums = []
    seen = set()

    def add_album(raw_album: Optional[Dict[str, Any]]):
        if not isinstance(raw_album, dict):
            return
        album_id = server._recommendation_trim_text(raw_album.get("id"))
        title = server._recommendation_trim_text(raw_album.get("title"))
        artist = server._recommendation_trim_text(raw_album.get("artist"))
        key = album_id or f"{server._normalize_text(title)}|{server._normalize_text(artist)}"
        if not key or key in seen:
            return
        seen.add(key)
        albums.append(raw_album)

    album_id = server._recommendation_trim_text(track.get("album_id"))
    album_title = server._recommendation_trim_text(track.get("album"))
    if album_id:
        add_album(
            {
                "id": album_id,
                "title": album_title or "Unknown Album",
                "artist": track.get("channel") or track.get("artist"),
                "thumbnail": track.get("thumbnail"),
            }
        )
    elif album_title:
        add_album(
            {
                "id": None,
                "title": album_title,
                "artist": track.get("channel") or track.get("artist"),
                "thumbnail": track.get("thumbnail"),
            }
        )

    search_query = _recommendation_anchor_query(track, include_album=True)
    if search_query:
        for album in server._assistant_tool_search_albums(search_query, max(limit * 2, 4)):
            add_album(album)
            if len(albums) >= limit:
                break

    return albums[:limit]


def _recommendation_candidate_sources_for_track(track: Optional[Dict[str, Any]]):
    server = _server()
    if not isinstance(track, dict):
        return []

    track_id = server._recommendation_trim_text(track.get("id"))
    artist_name = server._recommendation_trim_text(
        track.get("channel") or track.get("author") or track.get("artist")
    )
    futures = {}

    if track_id:
        futures["similar"] = server.recommendation_executor.submit(
            server._assistant_tool_get_similar_tracks,
            track.get("id"),
            12,
        )
        futures["collaborative"] = server.recommendation_executor.submit(
            server._recommendation_collaborative_neighbor_tracks,
            track_id,
            10,
        )

    if artist_name:
        futures["artist_seed"] = server.recommendation_executor.submit(
            search_artist_seed_tracks,
            artist_name,
            8,
        )

    def fetch_album_context():
        album_tracks = []
        for album in _recommendation_album_candidates_for_track(track, limit=2):
            album_id = server._recommendation_trim_text(album.get("id"))
            if not album_id:
                continue
            album_details = server._assistant_tool_get_album_details(album_id)
            album_tracks.extend(album_details.get("tracks") or [])
        return album_tracks

    futures["album_context"] = server.recommendation_executor.submit(fetch_album_context)

    source_results = {
        "similar": [],
        "collaborative": [],
        "artist_seed": [],
        "album_context": [],
        "fallback_context": [],
    }
    for source_name, future in futures.items():
        try:
            source_results[source_name] = future.result(
                timeout=server.RECOMMENDATION_CANDIDATE_SOURCE_TIMEOUT_SECONDS
            ) or []
        except Exception:
            source_results[source_name] = []

    if not (
        source_results["similar"]
        or source_results["collaborative"]
        or source_results["artist_seed"]
    ):
        fallback_tracks = [item for item, _score in server._fallback_home_candidates(16)]
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


def _recommendation_continue_listening_row(profile):
    server = _server()
    anchor_tracks = []
    seen_anchor_ids = set()
    for track in [
        *(profile.get("last_played_tracks") or []),
        *(profile.get("recent_track_snapshots") or []),
    ]:
        track_id = server._recommendation_trim_text(track.get("id"))
        if not track_id or track_id in seen_anchor_ids:
            continue
        seen_anchor_ids.add(track_id)
        anchor_tracks.append(track)
        if len(anchor_tracks) >= 3:
            break

    if not anchor_tracks:
        return None

    candidates = []
    for index, anchor_track in enumerate(anchor_tracks):
        anchor_title = anchor_track.get("title") or "that song"
        for source_name, source_tracks, base_score in _recommendation_candidate_sources_for_track(anchor_track):
            candidates.extend(
                server._recommendation_candidates_from_tracks(
                    source_tracks,
                    f"continue_listening:{source_name}",
                    base_score - (index * 0.22),
                    f"Continues from {anchor_title}.",
                )
            )

    if not candidates:
        return None

    return {
        "title": "Continue the vibe",
        "kind": "continue_listening",
        "candidates": candidates,
    }


def _recommendation_because_you_played_row(profile):
    server = _server()
    anchor_track = (
        (profile.get("last_played_tracks") or [None])[0]
        or (profile.get("recent_track_snapshots") or [None])[0]
    )
    anchor_ids = profile["recent_track_ids"][:2] or profile["top_track_ids"][:2]
    if not anchor_track and not anchor_ids:
        return None
    anchor_id = (anchor_track or {}).get("id") or anchor_ids[0]
    if anchor_track:
        anchor_title = anchor_track.get("title") or "that song"
        anchor_artist = anchor_track.get("channel") or ""
    else:
        fetched_anchor = server._recommendation_fetch_tracks_for_ids([anchor_id], limit=1)
        anchor_title = fetched_anchor[0]["title"] if fetched_anchor else "that song"
        anchor_artist = fetched_anchor[0]["channel"] if fetched_anchor else ""
    title = (
        f"Because you played {anchor_artist}"
        if anchor_artist
        else f"Because you played {anchor_title}"
    )
    candidates = []
    anchor_track_payload = anchor_track or {
        "id": anchor_id,
        "title": anchor_title,
        "channel": anchor_artist,
        "author": anchor_artist,
        "artist": anchor_artist,
    }
    for source_name, source_tracks, base_score in _recommendation_candidate_sources_for_track(anchor_track_payload):
        candidates.extend(
            server._recommendation_candidates_from_tracks(
                source_tracks,
                f"because_you_played:{source_name}",
                base_score,
                f"Shaped from {anchor_title}.",
            )
        )
    return {
        "title": title,
        "kind": "because_you_played",
        "candidates": candidates,
    }


def _recommendation_listeners_like_you_row(profile):
    server = _server()
    collaborative = profile.get("collaborative") or {}
    candidate_track_ids = collaborative.get("candidate_track_ids") or []
    if not candidate_track_ids:
        return None
    tracks = server._recommendation_fetch_tracks_for_ids(candidate_track_ids, limit=18)
    if not tracks:
        return None
    return {
        "title": "Listeners like you also played",
        "kind": "listeners_like_you",
        "candidates": server._recommendation_candidates_from_tracks(
            tracks,
            "collaborative",
            5.0,
            "Learned from collaborative listening patterns and your recent taste.",
        ),
    }


def _recommendation_frequently_listened_row(profile):
    server = _server()
    tracks = [dict(track) for track in (profile.get("top_track_snapshots") or [])]
    seen_ids = {
        track.get("id")
        for track in tracks
        if track.get("id")
    }
    missing_ids = [
        track_id
        for track_id in profile["top_track_ids"]
        if track_id not in seen_ids
    ]
    if len(tracks) < 18 and missing_ids:
        tracks.extend(
            server._recommendation_fetch_tracks_for_ids(
                missing_ids,
                limit=max(0, 18 - len(tracks)),
            )
        )
    if not tracks:
        return None
    return {
        "title": "Frequently listened",
        "kind": "frequently_listened",
        "candidates": server._recommendation_candidates_from_tracks(
            tracks,
            "frequently_listened",
            5.2,
            "You keep coming back to this one.",
        ),
    }


def _recommendation_rediscover_row(profile):
    server = _server()
    older_ids = [
        track_id
        for track_id in profile["top_track_ids"]
        if track_id not in profile["recent_track_ids"]
    ]
    tracks = server._recommendation_fetch_tracks_for_ids(older_ids, limit=14)
    candidates = server._recommendation_candidates_from_tracks(
        tracks,
        "rediscovery",
        4.1,
        "A favorite worth bringing back.",
    )

    if len(candidates) < 6:
        seen_ids = {
            server._recommendation_trim_text((candidate.get("track") or {}).get("id"))
            for candidate in candidates
        }
        for track in (profile.get("top_track_snapshots") or []) + (profile.get("last_played_tracks") or []):
            track_id = server._recommendation_trim_text(track.get("id"))
            if not track_id or track_id in profile["recent_track_ids"]:
                continue
            if track_id not in seen_ids:
                candidates.extend(
                    server._recommendation_candidates_from_tracks(
                        [track],
                        "rediscovery:history",
                        3.7,
                        "A favorite worth bringing back.",
                    )
                )
                seen_ids.add(track_id)
            for source_name, source_tracks, base_score in _recommendation_candidate_sources_for_track(track):
                filtered_tracks = [
                    source_track
                    for source_track in source_tracks
                    if server._recommendation_trim_text(source_track.get("id")) not in profile["recent_track_ids"]
                ]
                candidates.extend(
                    server._recommendation_candidates_from_tracks(
                        filtered_tracks,
                        f"rediscovery:{source_name}",
                        max(base_score - 0.35, 2.6),
                        "A favorite worth bringing back.",
                    )
                )
            if len(candidates) >= 12:
                break

    if not candidates and profile["top_track_ids"]:
        fallback_tracks = server._recommendation_fetch_tracks_for_ids(
            profile["top_track_ids"],
            limit=10,
        )
        candidates = server._recommendation_candidates_from_tracks(
            fallback_tracks,
            "rediscovery:fallback",
            3.4,
            "A favorite worth bringing back.",
        )
    if not candidates:
        return None
    return {
        "title": "Rediscover these",
        "kind": "rediscover",
        "candidates": candidates,
    }


def _recommendation_deep_cuts_row(profile):
    server = _server()
    candidates = []
    for artist_hint in profile["top_artists"][:3]:
        artists = server._assistant_tool_search_artists(artist_hint, 2)
        if not artists:
            continue
        artist_id = artists[0].get("id")
        if not artist_id:
            continue
        try:
            artist_payload = server._build_artist_details_payload(artist_id)
        except Exception:
            artist_payload = {}
        album_ids = [
            album.get("id")
            for album in artist_payload.get("albums", [])[1:5]
            if album.get("id")
        ]
        deep_cut_tracks = []
        for album_id in album_ids:
            details = server._assistant_tool_get_album_details(album_id)
            deep_cut_tracks.extend(details.get("tracks") or [])
        if not deep_cut_tracks:
            deep_cut_tracks = server._assistant_tool_search_tracks(
                f"{artist_hint} album tracks",
                14,
            )
        if len(deep_cut_tracks) < 6:
            deep_cut_tracks.extend(search_artist_seed_tracks(artist_hint, 8))
        candidates.extend(
            server._recommendation_candidates_from_tracks(
                deep_cut_tracks,
                "deep_cuts",
                3.6,
                f"Pulled from deeper {artist_hint} territory.",
            )
        )
    if not candidates:
        return None
    return {
        "title": "Deep cuts for you",
        "kind": "deep_cuts",
        "candidates": candidates,
    }


def _recommendation_offline_ready_row(profile):
    server = _server()
    track_ids = profile["offline_track_ids"] or profile["library_track_ids"]
    tracks = server._recommendation_fetch_tracks_for_ids(track_ids, limit=18)
    if not tracks:
        return None
    return {
        "title": "Ready offline",
        "kind": "offline_ready",
        "candidates": server._recommendation_candidates_from_tracks(
            tracks,
            "offline_ready",
            4.8,
            "Ready even when you go offline.",
        ),
    }


def _recommendation_quiet_picks_row(profile):
    server = _server()
    quiet_query = _recommendation_quiet_base_query(profile)
    tracks = []
    candidates = []
    seen_signatures = set()
    used_queries = []
    profile_signal_candidates = 0
    anchor_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
            *(profile.get("top_track_snapshots") or []),
        ],
        3,
    )
    for anchor_track in anchor_tracks:
        for source_name, source_tracks, base_score in _recommendation_candidate_sources_for_track(anchor_track):
            if source_name not in {"similar", "collaborative", "artist_seed"}:
                continue
            deduped_source_tracks = []
            for source_track in source_tracks:
                signature = server._recommendation_track_signature(source_track)
                if not signature or signature in seen_signatures:
                    continue
                seen_signatures.add(signature)
                deduped_source_tracks.append(source_track)
                tracks.append(source_track)
                if len(tracks) >= 120:
                    break
            if deduped_source_tracks:
                profile_signal_candidates += len(deduped_source_tracks)
                candidates.extend(
                    server._recommendation_candidates_from_tracks(
                        deduped_source_tracks,
                        f"quiet_profile:{source_name}",
                        max(base_score - 1.25, 2.4),
                        "Calmer picks inferred from your recent listening profile.",
                    )
                )
            if len(tracks) >= 120:
                break
    collaborative_ids = (
        (profile.get("collaborative") or {}).get("candidate_track_ids") or []
    )
    collaborative_tracks = server._recommendation_fetch_tracks_for_ids(
        collaborative_ids,
        limit=12,
    )
    collaborative_deduped = []
    for track in collaborative_tracks:
        signature = server._recommendation_track_signature(track)
        if not signature or signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        collaborative_deduped.append(track)
        tracks.append(track)
        if len(tracks) >= 132:
            break
    if collaborative_deduped:
        profile_signal_candidates += len(collaborative_deduped)
        candidates.extend(
            server._recommendation_candidates_from_tracks(
                collaborative_deduped,
                "quiet_profile:collaborative",
                2.9,
                "Calmer picks inferred from similar listeners.",
            )
        )
    fallback_tracks = _recommendation_taste_filtered_tracks(
        [
            track
            for track, _base_score in server._fallback_home_candidates(
                server.RECOMMENDATION_QUIET_FALLBACK_LIMIT
            )
        ],
        profile,
        limit=server.RECOMMENDATION_QUIET_FALLBACK_LIMIT,
    )
    fallback_deduped = []
    for track in fallback_tracks:
        signature = server._recommendation_track_signature(track)
        if not signature or signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        fallback_deduped.append(track)
        tracks.append(track)
        if len(tracks) >= 160:
            break
    if fallback_deduped:
        candidates.extend(
            server._recommendation_candidates_from_tracks(
                fallback_deduped,
                "quiet_fallback",
                2.45,
                "Fallback calm picks while personalization warms up.",
            )
        )
    if not tracks:
        return None
    row_strategy = "personalized"
    fallback_reason = ""
    if fallback_deduped and profile_signal_candidates > 0:
        row_strategy = "hybrid"
        fallback_reason = "sparse_quiet_signal"
    elif fallback_deduped and profile_signal_candidates <= 0:
        row_strategy = "fallback"
        fallback_reason = "sparse_profile_signal"
    return {
        "title": "Quiet picks",
        "kind": "quiet_picks",
        "quiet_query": quiet_query,
        "used_queries": used_queries,
        "row_strategy": row_strategy,
        "fallback_reason": fallback_reason,
        "candidates": candidates
        or server._recommendation_candidates_from_tracks(
            tracks,
            "quiet_picks",
            3.4,
            "Built from calmer picks inferred from your listening profile.",
        ),
    }


def _recommendation_recommended_albums_row(profile):
    server = _server()
    albums = []
    seen = set()

    def add_album(raw_album, base_score: float):
        if not isinstance(raw_album, dict):
            return
        album_id = server._recommendation_trim_text(raw_album.get("id"))
        title = server._recommendation_trim_text(raw_album.get("title"))
        artist = server._recommendation_trim_text(raw_album.get("artist"))
        key = album_id or f"{server._normalize_text(title)}|{server._normalize_text(artist)}"
        if not key or key in seen:
            return
        seen.add(key)
        albums.append(
            {
                "id": album_id or None,
                "title": title or "Unknown Album",
                "artist": artist or "Unknown Artist",
                "thumbnail": raw_album.get("thumbnail"),
                "year": raw_album.get("year") or "",
                "track_count": raw_album.get("track_count") or raw_album.get("trackCount") or 0,
                "generator_score": round(base_score, 3),
            }
        )

    for index, album_hint in enumerate(profile.get("top_albums") or []):
        for offset, album in enumerate(server._assistant_tool_search_albums(album_hint, 5)):
            add_album(album, 4.2 - (index * 0.28) - (offset * 0.16))
            if len(albums) >= 18:
                break
        if len(albums) >= 18:
            break

    for index, artist_hint in enumerate(profile.get("top_artists") or []):
        direct_artists = server._assistant_tool_search_artists_direct(artist_hint, 1)
        if direct_artists:
            artist_id = server._recommendation_trim_text(direct_artists[0].get("id"))
            if artist_id:
                try:
                    artist_payload = server._build_artist_details_payload(artist_id)
                except Exception:
                    artist_payload = {}
                for offset, album in enumerate(artist_payload.get("albums") or []):
                    add_album(album, 4.0 - (index * 0.22) - (offset * 0.18))
                    if len(albums) >= 18:
                        break
        if len(albums) < 12:
            for offset, album in enumerate(server._assistant_tool_search_albums(f"{artist_hint} album", 4)):
                add_album(album, 3.6 - (index * 0.18) - (offset * 0.14))
                if len(albums) >= 18:
                    break
        if len(albums) >= 18:
            break

    if len(albums) < 12:
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
        )
        if server._normalize_text(album.get("title") or "") in top_album_names:
            ranking_score += 0.8
        if server._normalize_text(album.get("artist") or "") in top_artist_names:
            ranking_score += 0.6
        album["generator_score"] = round(ranking_score, 3)
        album["ml_similarities"] = {
            name: round(value, 4)
            for name, value in similarities.items()
        }

    albums.sort(key=lambda item: item.get("generator_score", 0), reverse=True)
    return {
        "title": "Recommended albums",
        "kind": "recommended_albums",
        "item_type": "album",
        "items": albums[:18],
    }


def _recommendation_trending_row(profile):
    server = _server()
    candidates = []
    seen_signatures = set()
    used_sources = set()

    def add_candidate_pool(tracks, generator_name: str, base_score: float, reason: str):
        for index, track in enumerate(tracks or []):
            signature = server._recommendation_track_signature(track)
            if not signature or signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            candidate = server._recommendation_candidate(
                track,
                generator_name,
                max(base_score - (index * 0.12), 1.0),
                reason,
            )
            if candidate is not None:
                candidates.append(candidate)
                used_sources.add(generator_name)

    collaborative_ids = (
        (profile.get("collaborative") or {}).get("candidate_track_ids") or []
    )
    if collaborative_ids:
        add_candidate_pool(
            server._recommendation_fetch_tracks_for_ids(collaborative_ids, limit=18),
            "trending_collaborative",
            4.6,
            "Popular with listeners who overlap with your taste.",
        )
    for index, artist_name in enumerate((profile.get("top_artists") or [])[:3]):
        artist_seed_tracks = search_artist_seed_tracks(artist_name, 8)
        add_candidate_pool(
            artist_seed_tracks,
            "trending_artist_neighbor",
            max(3.8 - (index * 0.18), 2.8),
            f"Trending around artists you keep returning to like {artist_name}.",
        )

    fallback_tracks = _recommendation_taste_filtered_tracks(
        [track for track, _base_score in server._fallback_home_candidates(20)],
        profile,
        limit=18,
    )
    add_candidate_pool(
        fallback_tracks,
        "trending_for_you",
        2.1,
        "Trending, filtered through your taste.",
    )
    if not candidates:
        return None
    row_strategy = "personalized"
    fallback_reason = ""
    personalized_sources = {
        source_name
        for source_name in used_sources
        if source_name != "trending_for_you"
    }
    if fallback_tracks and personalized_sources:
        row_strategy = "hybrid"
        fallback_reason = "supplemental_trending_fallback"
    elif fallback_tracks and not personalized_sources:
        row_strategy = "fallback"
        fallback_reason = "sparse_trending_signals"
    return {
        "title": "Trending for you",
        "kind": "trending_for_you",
        "candidates": candidates,
        "row_strategy": row_strategy,
        "fallback_reason": fallback_reason,
    }


def _recommendation_apply_quiet_row_runtime_fields(finalized, row_seed):
    server = _server()
    finalized["base_query"] = server._recommendation_trim_text(
        row_seed.get("quiet_query")
    )
    finalized["extension_cycle"] = 0
    finalized["can_extend"] = len(row_seed.get("candidates") or []) > len(
        finalized.get("items") or []
    )
    initial_used_queries = [
        query
        for query in (row_seed.get("used_queries") or [])
        if server._recommendation_trim_text(query)
    ]
    if not initial_used_queries:
        quiet_query = server._recommendation_trim_text(row_seed.get("quiet_query"))
        initial_used_queries = [quiet_query] if quiet_query else []
    finalized["used_queries"] = initial_used_queries
    finalized["used_signatures"] = [
        signature
        for signature in (
            server._recommendation_track_signature(track)
            for track in (finalized.get("items") or [])
        )
        if signature
    ]
    return finalized


def _recommendation_taste_filtered_tracks(tracks, profile, *, limit: int):
    server = _server()
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


def _recommendation_required_row_fallback_seed(row_kind: str, profile):
    server = _server()
    fallback_tracks = _recommendation_taste_filtered_tracks(
        [track for track, _score in server._fallback_home_candidates(28)],
        profile,
        limit=24,
    )
    fallback_candidates = server._recommendation_candidates_from_tracks(
        fallback_tracks,
        f"required_fallback:{row_kind}",
        2.25,
        "Fallback picks while personalization data is warming up.",
    )

    if row_kind == "continue_listening":
        anchor_tracks = server._recommendation_unique_snapshot_tracks(
            [
                *(profile.get("last_played_tracks") or []),
                *(profile.get("recent_track_snapshots") or []),
            ],
            2,
        )
        candidates = []
        for anchor_track in anchor_tracks:
            for source_name, source_tracks, base_score in _recommendation_candidate_sources_for_track(anchor_track):
                if source_name not in {"similar", "collaborative", "artist_seed"}:
                    continue
                candidates.extend(
                    server._recommendation_candidates_from_tracks(
                        source_tracks,
                        f"continue_required:{source_name}",
                        max(base_score - 1.0, 2.3),
                        "Continues from your recent listening.",
                    )
                )
        if candidates and fallback_candidates:
            candidates.extend(fallback_candidates[:12])
            return {
                "title": "Continue the vibe",
                "kind": "continue_listening",
                "candidates": candidates,
                "row_strategy": "hybrid",
                "fallback_reason": "required_row_missing",
            }
        return {
            "title": "Continue the vibe",
            "kind": "continue_listening",
            "candidates": fallback_candidates,
            "row_strategy": "fallback",
            "fallback_reason": "required_row_missing",
        }

    if row_kind == "because_you_played":
        anchor_track = (
            (profile.get("last_played_tracks") or [None])[0]
            or (profile.get("recent_track_snapshots") or [None])[0]
            or (profile.get("top_track_snapshots") or [None])[0]
        )
        candidates = []
        if isinstance(anchor_track, dict):
            for source_name, source_tracks, base_score in _recommendation_candidate_sources_for_track(anchor_track):
                if source_name not in {"similar", "collaborative"}:
                    continue
                candidates.extend(
                    server._recommendation_candidates_from_tracks(
                        source_tracks,
                        f"because_required:{source_name}",
                        max(base_score - 0.85, 2.45),
                        "Because you played similar music recently.",
                    )
                )
        if candidates and fallback_candidates:
            candidates.extend(fallback_candidates[:10])
            return {
                "title": "Because you played recently",
                "kind": "because_you_played",
                "candidates": candidates,
                "row_strategy": "hybrid",
                "fallback_reason": "required_row_missing",
            }
        return {
            "title": "Because you played recently",
            "kind": "because_you_played",
            "candidates": fallback_candidates,
            "row_strategy": "fallback",
            "fallback_reason": "required_row_missing",
        }

    if row_kind == "trending_for_you":
        candidates = []
        collaborative_ids = (
            (profile.get("collaborative") or {}).get("candidate_track_ids") or []
        )
        if collaborative_ids:
            collaborative_tracks = server._recommendation_fetch_tracks_for_ids(
                collaborative_ids,
                limit=12,
            )
            candidates.extend(
                server._recommendation_candidates_from_tracks(
                    collaborative_tracks,
                    "trending_required:collaborative",
                    3.4,
                    "Trending among listeners similar to you.",
                )
            )
        candidates.extend(
            server._recommendation_candidates_from_tracks(
                fallback_tracks,
                "trending_required:fallback",
                2.4,
                "Trending picks filtered by your profile.",
            )
        )
        return {
            "title": "Trending for you",
            "kind": "trending_for_you",
            "candidates": candidates,
            "row_strategy": "hybrid" if collaborative_ids else "fallback",
            "fallback_reason": "required_row_missing",
        }

    if row_kind == "quiet_picks":
        quiet_query = _recommendation_quiet_base_query(profile)
        candidates = []
        anchor_tracks = server._recommendation_unique_snapshot_tracks(
            [
                *(profile.get("last_played_tracks") or []),
                *(profile.get("recent_track_snapshots") or []),
                *(profile.get("top_track_snapshots") or []),
            ],
            2,
        )
        for anchor_track in anchor_tracks:
            for source_name, source_tracks, base_score in _recommendation_candidate_sources_for_track(anchor_track):
                if source_name not in {"similar", "collaborative", "artist_seed"}:
                    continue
                candidates.extend(
                    server._recommendation_candidates_from_tracks(
                        source_tracks,
                        f"quiet_required:{source_name}",
                        max(base_score - 1.05, 2.3),
                        "Quieter picks inferred from your listening profile.",
                    )
                )
        collaborative_ids = (
            (profile.get("collaborative") or {}).get("candidate_track_ids") or []
        )
        if collaborative_ids:
            candidates.extend(
                server._recommendation_candidates_from_tracks(
                    server._recommendation_fetch_tracks_for_ids(
                        collaborative_ids,
                        limit=10,
                    ),
                    "quiet_required:collaborative",
                    2.7,
                    "Quieter picks inferred from similar listeners.",
                )
            )
        candidates.extend(
            server._recommendation_candidates_from_tracks(
                fallback_tracks[: server.RECOMMENDATION_QUIET_FALLBACK_LIMIT],
                "quiet_required:fallback",
                2.2,
                "Calm fallback picks while personalization stabilizes.",
            )
        )
        return {
            "title": "Quiet picks",
            "kind": "quiet_picks",
            "quiet_query": quiet_query,
            "used_queries": [],
            "candidates": candidates,
            "row_strategy": "hybrid" if candidates else "fallback",
            "fallback_reason": "required_row_missing",
        }

    return None


def _recommendation_build_row_seed(builder, profile):
    started_at = time.perf_counter()
    try:
        row_seed = builder(profile)
        builder_error = ""
    except Exception as exc:
        row_seed = None
        builder_error = str(exc)
    builder_ms = int((time.perf_counter() - started_at) * 1000)
    return {
        "row_seed": row_seed,
        "builder_ms": builder_ms,
        "error": builder_error,
    }
