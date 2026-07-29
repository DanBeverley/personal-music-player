from __future__ import annotations

from typing import Any, Dict

from ..domain.catalog import (
    cache_artists,
    normalize_artist_name,
    normalized_artist_payload,
)
from ..search.intelligence import (
    load_catalog_artist_records,
    remember_catalog_entity,
)
from ..storage.artist_artwork import (
    notify_artist_metadata_updated,
    schedule_artist_artwork_cache,
)
from .server_adapter import adapt_detail_server


def _persist_artist_details(server: Any, payload: Dict[str, Any]) -> None:
    artist_id = str(payload.get("id") or "").strip()
    if not artist_id:
        return
    artist = normalized_artist_payload(
        {
            "id": artist_id,
            "name": payload.get("name") or "Unknown Artist",
            "thumbnail": payload.get("thumbnail") or "",
            "description": payload.get("description") or "",
            "stats": dict(payload.get("stats") or {}),
            "source_authority": "ytmusic_artist_detail",
        }
    )

    def persist(record: Dict[str, Any]) -> None:
        cache_artists([record])
        remember_catalog_entity(
            server,
            user_scope_id="global",
            query=str(record.get("name") or ""),
            entity_type="artist",
            item=record,
            confidence=0.98,
            event_weight=0.0,
            event_type="artist_metadata",
            source="ytmusic_artist_detail",
        )
        notify_artist_metadata_updated(record)

    try:
        persist(artist)
        schedule_artist_artwork_cache(server, artist, on_cached=persist)
    except Exception:
        return


def _with_persisted_artist_artwork(
    server: Any,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    artist = dict(payload)
    name = str(artist.get("name") or "").strip()
    if not name:
        return artist
    persisted = load_catalog_artist_records(
        server,
        artist_names=[name],
    ).get(normalize_artist_name(name)) or {}
    thumbnail = str(persisted.get("thumbnail") or "").strip()
    if thumbnail.startswith("/artist_artwork/"):
        artist["thumbnail"] = thumbnail
    if persisted.get("canonical_artist_id"):
        artist["canonical_artist_id"] = persisted["canonical_artist_id"]
    return artist


def build_artist_details_payload(
    server: Any,
    artist_id: str,
    *,
    enrich_related: bool = True,
    lightweight: bool = False,
) -> Dict[str, Any]:
    server = adapt_detail_server(server)
    cache_key = server.trim_text(artist_id)
    if cache_key:
        cache_key = (
            f"{cache_key}:"
            f"{'expanded' if enrich_related else 'basic'}:"
            f"{'light' if lightweight else 'full'}"
        )
    if cache_key:
        cached = server.cache_lookup("artist", cache_key)
        if cached is not None:
            cached_payload = _with_persisted_artist_artwork(
                server,
                dict(cached),
            )
            _persist_artist_details(server, cached_payload)
            return cached_payload

    raw_artist = server.ytmusic.get_artist(artist_id)
    artist = dict(raw_artist) if isinstance(raw_artist, dict) else {}
    name = artist.get("name") or "Unknown Artist"
    songs_section = artist.get("songs") or {}
    album_section = artist.get("albums") or {}
    related_section = artist.get("related") or {}

    try:
        top_songs = server.normalize_artist_song_entries(
            songs_section.get("results") or [],
            fallback_artist=name,
        )
    except Exception:
        top_songs = []

    try:
        albums = server.normalize_artist_album_entries(
            album_section.get("results") or [],
            fallback_artist=name,
        )
    except Exception:
        albums = []
    album_browse_id = album_section.get("browseId")
    album_params = album_section.get("params")
    if album_browse_id and album_params and not lightweight:
        try:
            more_albums = server.ytmusic.get_artist_albums(
                album_browse_id,
                album_params,
                limit=12,
            )
        except Exception:
            more_albums = []
        try:
            normalized_more_albums = server.normalize_artist_album_entries(
                more_albums,
                fallback_artist=name,
            )
        except Exception:
            normalized_more_albums = []
        for album in normalized_more_albums:
            album_id = album.get("id")
            if album_id and any(existing.get("id") == album_id for existing in albums):
                continue
            albums.append(album)
            if len(albums) >= 12:
                break

    try:
        normalized_related = server.normalize_artist_results(
            related_section.get("results") or []
        )
    except Exception:
        normalized_related = []
    if lightweight:
        related_artists = normalized_related
    else:
        try:
            related_artists = server.rank_artist_detail_related_artists(
                {
                    "id": artist_id,
                    "name": name,
                    "description": artist.get("description") or "",
                    "thumbnail": server.extract_thumbnail(artist),
                },
                top_songs,
                normalized_related,
                enrich_related=enrich_related,
            )
        except Exception:
            related_artists = normalized_related if enrich_related else []

    try:
        thumbnail = server.extract_thumbnail(artist)
    except Exception:
        thumbnail = None
    try:
        description = server.summarize_artist_description(
            artist.get("description") or ""
        )
    except Exception:
        description = artist.get("description") or ""
    try:
        stats = server.normalize_artist_stats(artist)
    except Exception:
        stats = {}

    payload = {
        "status": "success",
        "id": artist_id,
        "name": name,
        "description": description,
        "full_description": artist.get("description") or "",
        "thumbnail": thumbnail,
        "stats": stats,
        "top_songs": top_songs[:12],
        "albums": albums[:12],
        "related_artists": related_artists[:12],
    }
    payload = _with_persisted_artist_artwork(server, payload)
    if cache_key:
        server.cache_store("artist", cache_key, payload)
    _persist_artist_details(server, payload)
    return payload


def build_track_details_payload(server: Any, video_id: str) -> Dict[str, Any]:
    server = adapt_detail_server(server)
    cache_key = server.trim_text(video_id)
    if cache_key:
        cached = server.cache_lookup("track", cache_key)
        if cached is not None:
            return dict(cached)

    release_date = ""
    artist = ""
    album_title = ""
    album_id = None

    try:
        song = server.ytmusic.get_song(video_id)
        if "microformat" in song and "microformatDataRenderer" in song["microformat"]:
            release_date = song["microformat"]["microformatDataRenderer"].get("publishDate", "")
        vd = song.get("videoDetails", {})
        artist = vd.get("author", "")
        song_album = server.extract_album_info(song) or server.extract_album_info(vd)
        if song_album:
            album_title = song_album.get("title") or ""
            album_id = song_album.get("id")
    except Exception:
        pass

    watch = server.ytmusic.get_watch_playlist(videoId=video_id)
    video_details = watch.get("videoDetails", {})
    track_title = video_details.get("title") or ""
    if not artist:
        artist = server.extract_artist(video_details)
    if not album_title:
        looked_up_album = server.lookup_album_for_song(video_id, track_title, artist)
        if looked_up_album:
            album_title = looked_up_album.get("title") or ""
            album_id = looked_up_album.get("id")

    similar_tracks = []
    for track in watch.get("tracks", []):
        if track.get("videoId") == video_id or not track.get("videoId"):
            continue
        similar_tracks.append({
            "id": track["videoId"],
            "title": track.get("title"),
            "duration": track.get("length") or track.get("duration_seconds") or 0,
            "thumbnail": server.extract_thumbnail(track),
            "channel": server.extract_artist(track),
            "album": (server.extract_album_info(track) or {}).get("title"),
            "album_id": (server.extract_album_info(track) or {}).get("id"),
        })
    payload = {
        "status": "success",
        "video_id": video_id,
        "title": track_title,
        "author": artist,
        "thumbnail": server.extract_thumbnail(video_details),
        "duration": video_details.get("lengthSeconds"),
        "release_date": release_date,
        "album": album_title,
        "album_title": album_title,
        "album_id": album_id,
        "lyrics_available": bool(watch.get("lyrics")),
        "similar_tracks": similar_tracks,
    }
    if cache_key:
        server.cache_store("track", cache_key, payload)
    return payload


def build_album_details_payload(server: Any, album_id: str) -> Dict[str, Any]:
    server = adapt_detail_server(server)
    cache_key = server.trim_text(album_id)
    if cache_key:
        cached = server.cache_lookup("album", cache_key)
        if cached is not None:
            return dict(cached)

    album = server.ytmusic.get_album(album_id)
    album_thumbnail = server.extract_thumbnail(album)
    album_artist = server.extract_artist(album)
    tracks = []

    for entry in album.get("tracks", []):
        video_id = entry.get("videoId")
        if not video_id:
            continue
        tracks.append({
            "id": video_id,
            "title": entry.get("title"),
            "duration": server.parse_duration_seconds(
                entry.get("duration_seconds")
                or entry.get("duration")
                or entry.get("length")
            ),
            "thumbnail": server.extract_thumbnail(entry) or album_thumbnail,
            "channel": server.extract_artist(entry) or album_artist,
            "album": album.get("title"),
            "album_title": album.get("title"),
            "album_id": album_id,
            "year": album.get("year") or "",
        })

    payload = {
        "status": "success",
        "id": album_id,
        "title": album.get("title"),
        "artist": album_artist,
        "thumbnail": album_thumbnail,
        "year": album.get("year") or "",
        "track_count": len(tracks),
        "tracks": tracks,
    }
    if cache_key:
        server.cache_store("album", cache_key, payload)
    return payload
