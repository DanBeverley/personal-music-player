from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from .details.detail_runtime import (
    build_album_details_payload as detail_build_album_details_payload,
    build_track_details_payload as detail_build_track_details_payload,
)


def assistant_track_from_context(server: Any, track):
    if isinstance(track, BaseModel):
        track = track.model_dump() if hasattr(track, "model_dump") else track.dict()
    raw = dict(track or {})
    track_id = (raw.get("id") or "").strip()
    if not track_id:
        return None
    artist = raw.get("artist") or raw.get("channel") or ""
    return {
        "id": track_id,
        "title": raw.get("title") or "Unknown Track",
        "channel": artist,
        "artist": artist,
        "album": raw.get("album"),
        "thumbnail": raw.get("thumbnail"),
        "duration": server.parse_duration_seconds(raw.get("duration")),
        "reason": raw.get("reason"),
    }


def assistant_all_context_tracks(server: Any, req: Any):
    groups = {
        "last_assistant_tracks": req.last_assistant_tracks,
        "last_playlist_draft_tracks": req.last_playlist_draft_tracks,
        "recent_assistant_tracks": req.recent_assistant_tracks,
    }
    normalized = {}
    for key, tracks in groups.items():
        normalized[key] = [
            track
            for track in (assistant_track_from_context(server, entry) for entry in tracks)
            if track is not None
        ]
    return normalized


def assistant_tool_search_tracks(server: Any, query: str, limit: int):
    query = (query or "").strip()
    if not query:
        return []
    from auralis_backend.search.runtime import search_tracks_direct

    return search_tracks_direct(query, limit, server=server)


def assistant_tool_search_albums(server: Any, query: str, limit: int):
    query = (query or "").strip()
    if not query:
        return []
    from auralis_backend.search.runtime import search_albums_direct

    return search_albums_direct(query, limit, server=server)


def assistant_tool_search_artists_direct(server: Any, query: str, limit: int):
    from auralis_backend.search.upstream_runtime import search_artists_direct

    return search_artists_direct(server, query, limit)


def artist_names_from_track_query(server: Any, query: str, limit: int):
    from auralis_backend.search.upstream_runtime import artist_names_from_track_query as _impl

    return _impl(server, query, limit)


def assistant_tool_search_artists(server: Any, query: str, limit: int):
    from auralis_backend.search.upstream_runtime import search_artists

    return search_artists(server, query, limit)


def build_track_details_payload(server: Any, video_id: str):
    return detail_build_track_details_payload(server, video_id)


def build_album_details_payload(server: Any, album_id: str):
    return detail_build_album_details_payload(server, album_id)


def assistant_tool_get_track_details(server: Any, video_id: str):
    video_id = (video_id or "").strip()
    if not video_id:
        return {}
    try:
        return build_track_details_payload(server, video_id)
    except Exception:
        return {}


def assistant_tool_get_album_details(server: Any, album_id: str):
    album_id = (album_id or "").strip()
    if not album_id:
        return {}
    try:
        return build_album_details_payload(server, album_id)
    except Exception:
        return {}


def assistant_tool_get_similar_tracks(server: Any, video_id: str, limit: int):
    details = assistant_tool_get_track_details(server, video_id)
    similar = details.get("similar_tracks")
    if not isinstance(similar, list):
        return []
    return similar[: max(1, min(limit, 12))]


def track_metadata_incomplete(server: Any, track: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(track, dict):
        return True
    title = server._normalize_text(track.get("title"))
    artist = server._normalize_text(
        track.get("channel") or track.get("author") or track.get("artist")
    )
    return not title or title == "unknown track" or not artist or artist == "unknown artist"


def merge_track_metadata(server: Any, primary: Dict[str, Any], fallback: Optional[Dict[str, Any]]):
    if fallback is None:
        return dict(primary)
    merged = dict(primary)
    for key in ("title", "thumbnail", "album", "album_id"):
        if not server._recommendation_trim_text(merged.get(key)):
            if server._recommendation_trim_text(fallback.get(key)):
                merged[key] = fallback.get(key)
    current_artist = server._recommendation_trim_text(
        merged.get("channel") or merged.get("author") or merged.get("artist")
    )
    fallback_artist = server._recommendation_trim_text(
        fallback.get("channel") or fallback.get("author") or fallback.get("artist")
    )
    if not current_artist or server._normalize_text(current_artist) == "unknown artist":
        if fallback_artist:
            merged["channel"] = fallback_artist
            merged["author"] = fallback_artist
            merged["artist"] = fallback_artist
    if server.parse_duration_seconds(merged.get("duration")) <= 0:
        fallback_duration = server.parse_duration_seconds(fallback.get("duration"))
        if fallback_duration > 0:
            merged["duration"] = fallback_duration
    return server.normalize_recommendation_track(merged) or merged


def recommendation_enrich_track_metadata(server: Any, track: Dict[str, Any]):
    normalized = server.normalize_recommendation_track(track) or dict(track)
    track_id = server._recommendation_trim_text(normalized.get("id"))
    if not track_id or not track_metadata_incomplete(server, normalized):
        return normalized
    details_track = server._recommendation_fetch_track_for_id(track_id)
    if details_track is None:
        return normalized
    enriched = merge_track_metadata(server, normalized, details_track)
    server._recommendation_store_cached_track(track_id, enriched)
    return enriched


def assistant_tool_get_user_taste_profile(_server: Any, req: Any):
    artist_counts = {}
    for entry in list(req.last_assistant_tracks) + list(req.recent_assistant_tracks):
        artist = (entry.artist or "").strip()
        if not artist:
            continue
        artist_counts[artist] = artist_counts.get(artist, 0) + 1

    top_artists = [
        artist
        for artist, _ in sorted(
            artist_counts.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:5]
    ]
    return {
        "recent_queries": list(req.recent_queries[:5]),
        "recent_track_ids": list(req.recent_track_ids[:8]),
        "playlist_names": [playlist.name for playlist in req.playlist_summaries[:6]],
        "library_track_count": len(req.library_tracks),
        "top_recent_artists": top_artists,
    }


def assistant_tool_use_context_tracks(
    server: Any,
    req: Any,
    source: str,
    count: int,
    artist_filter: Optional[str] = None,
):
    groups = assistant_all_context_tracks(server, req)
    selected = groups.get(source) or []
    if not selected and source != "recent_assistant_tracks":
        selected = groups.get("recent_assistant_tracks") or []
    if artist_filter:
        normalized_artist = server._normalize_text(artist_filter)
        filtered = [
            track
            for track in selected
            if normalized_artist in server._normalize_text(track.get("channel") or track.get("artist"))
        ]
        if filtered:
            selected = filtered
    count = max(1, min(count, 12))
    return selected[:count]


def assistant_tool_list_playlists(_server: Any, req: Any):
    return [
        {
            "id": playlist.id,
            "name": playlist.name,
            "track_count": playlist.track_count,
        }
        for playlist in req.playlist_summaries[:20]
    ]


def assistant_attach_reasons_runtime(_server: Any, tracks, reasons):
    reason_map = {}
    for row in reasons or []:
        if not isinstance(row, dict):
            continue
        track_id = row.get("id")
        reason = (row.get("reason") or "").strip()
        if track_id and reason:
            reason_map[track_id] = reason
    enriched = []
    for track in tracks:
        copy = dict(track)
        track_id = copy.get("id")
        if track_id in reason_map:
            copy["reason"] = reason_map[track_id]
        enriched.append(copy)
    return enriched


def assistant_store_turn_memory(
    server: Any,
    req: Any,
    response_payload: Dict[str, Any],
    selected_tracks: Optional[List[Dict[str, Any]]] = None,
    target_playlist: Optional[Dict[str, Any]] = None,
):
    scope_id = server._assistant_safe_scope_id(req.user_scope_id)
    server._assistant_store_memory(
        scope_id,
        "user_message",
        req.message,
        {
            "conversation_length": len(req.conversation),
        },
    )
    reply = (response_payload.get("reply") or "").strip()
    if reply:
        server._assistant_store_memory(
            scope_id,
            "assistant_reply",
            reply,
            {
                "action_type": response_payload.get("action_type"),
                "selected_track_ids": response_payload.get("selected_track_ids") or [],
            },
        )
    if selected_tracks:
        summary = "; ".join(
            f"{track.get('title') or 'Unknown Track'} - {track.get('channel') or track.get('artist') or 'Unknown Artist'}"
            for track in selected_tracks[:8]
        )
        server._assistant_store_memory(
            scope_id,
            "assistant_tracks",
            summary,
            {
                "track_ids": [
                    track.get("id")
                    for track in selected_tracks
                    if track.get("id")
                ],
                "action_type": response_payload.get("action_type"),
            },
        )
    playlist_name = (response_payload.get("playlist_name") or "").strip()
    if playlist_name:
        server._assistant_store_memory(
            scope_id,
            "playlist_intent",
            playlist_name,
            {
                "summary": response_payload.get("playlist_summary"),
                "target_playlist": target_playlist or {},
            },
        )


def assistant_initial_memory_queries(server: Any, req: Any):
    queries = [req.message]
    for entry in req.conversation[-4:]:
        if entry.content and entry.content.strip():
            queries.append(entry.content.strip())
    for query in req.recent_queries[:3]:
        if query and query.strip():
            queries.append(query.strip())
    deduped = []
    seen = set()
    for query in queries:
        normalized = server._normalize_text(query)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(query)
    return deduped[:6]


def assistant_merge_memory_hits(_server: Any, *groups):
    merged = []
    seen = set()
    for group in groups:
        for hit in group or []:
            hit_id = hit.get("id")
            if not hit_id or hit_id in seen:
                continue
            seen.add(hit_id)
            merged.append(hit)
    merged.sort(
        key=lambda item: (item.get("score", 0.0), item.get("created_at", 0.0)),
        reverse=True,
    )
    return merged[:8]


def assistant_playlist_options(server: Any, req: Any, preferred_names: Optional[List[str]] = None):
    available = list(req.playlist_summaries[:20])
    if not preferred_names:
        return [
            {
                "id": playlist.id,
                "name": playlist.name,
                "track_count": playlist.track_count,
            }
            for playlist in available[:12]
        ]

    resolved = []
    seen_ids = set()
    for name in preferred_names:
        playlist = server._playlist_lookup_by_name(available, name)
        if playlist is None or playlist.id in seen_ids:
            continue
        seen_ids.add(playlist.id)
        resolved.append(
            {
                "id": playlist.id,
                "name": playlist.name,
                "track_count": playlist.track_count,
            }
        )
    return resolved or [
        {
            "id": playlist.id,
            "name": playlist.name,
            "track_count": playlist.track_count,
        }
        for playlist in available[:12]
    ]


def assistant_model_for_request(server: Any, req: Any):
    return server.OLLAMA_THINKING_MODEL if req.thinking_mode else server.OLLAMA_FAST_MODEL


def assistant_langgraph_deps(server: Any, req: Any):
    selected_model = assistant_model_for_request(server, req)
    return {
        "initial_memory_queries": lambda request: assistant_initial_memory_queries(server, request),
        "query_memory": server._assistant_query_memory,
        "merge_memory_hits": lambda *groups: assistant_merge_memory_hits(server, *groups),
        "call_planner_structured": lambda messages, **kwargs: server._call_ollama_structured(
            messages,
            model_override=server.OLLAMA_PLANNER_MODEL,
            **kwargs,
        ),
        "call_response_structured": lambda messages, **kwargs: server._call_ollama_structured(
            messages,
            model_override=selected_model,
            **kwargs,
        ),
        "call_structured": lambda messages, **kwargs: server._call_ollama_structured(
            messages,
            model_override=selected_model,
            **kwargs,
        ),
        "call_chat": lambda messages, **kwargs: server._call_ollama_chat(
            messages,
            model_override=selected_model,
            **kwargs,
        ),
        "tool_search_tracks": server._assistant_tool_search_tracks,
        "tool_search_albums": server._assistant_tool_search_albums,
        "tool_search_artists": server._assistant_tool_search_artists,
        "tool_get_track_details": server._assistant_tool_get_track_details,
        "tool_get_album_details": server._assistant_tool_get_album_details,
        "tool_get_similar_tracks": server._assistant_tool_get_similar_tracks,
        "tool_get_user_taste_profile": server._assistant_tool_get_user_taste_profile,
        "tool_use_context_tracks": server._assistant_tool_use_context_tracks,
        "tool_list_playlists": server._assistant_tool_list_playlists,
        "all_context_tracks": server._assistant_all_context_tracks,
        "attach_reasons": server._assistant_attach_reasons_runtime,
        "playlist_lookup_by_name": server._playlist_lookup_by_name,
        "playlist_options": server._assistant_playlist_options,
        "store_turn_memory": server._assistant_store_turn_memory,
        "fallback_chat_reply": lambda request: assistant_fallback_chat_reply(
            server,
            request,
            model_override=selected_model,
        ),
        "model_name": selected_model,
        "planner_model_name": server.OLLAMA_PLANNER_MODEL,
    }


def assistant_fallback_chat_reply(server: Any, req: Any, model_override=None):
    messages = [
        {
            "role": "system",
            "content": (
                "You are Neatie, a warm conversational assistant. "
                "Reply naturally to the user's latest message. "
                "If they want comfort or conversation, be present and human. "
                "Do not force music suggestions unless they explicitly ask for music. "
                "Respond in 2 to 5 sentences."
            ),
        }
    ]
    for entry in req.conversation[-8:]:
        messages.append(
            {
                "role": "assistant" if entry.role == "assistant" else "user",
                "content": entry.content,
            }
        )
    messages.append({"role": "user", "content": req.message})
    reply = server._call_ollama_chat(
        messages,
        temperature=0.6,
        model_override=model_override,
    )
    reply = (reply or "").strip()
    if reply:
        return reply
    return "I'm here with you. Tell me a little more and I'll stay with you."
