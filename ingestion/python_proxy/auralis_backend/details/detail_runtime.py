from __future__ import annotations

from typing import Any, Dict
import json
import logging
import re
from urllib.parse import urlparse

from ..domain.catalog import (
    cache_albums,
    cache_artists,
    normalize_artist_name,
    normalized_album_payload,
    normalized_artist_payload,
    normalized_track_payload,
    artist_credit,
)
from ..search.intelligence import (
    load_catalog_artist_records,
    remember_catalog_entity,
)
from ..storage.artist_artwork import (
    attach_cached_entity_artwork,
    notify_artist_metadata_updated,
    schedule_entity_artwork_cache,
    schedule_artist_artwork_cache,
)
from .server_adapter import adapt_detail_server

_LOG = logging.getLogger(__name__)


def _optional_artist_section(section, load):
    try:
        return load()
    except Exception as exc:
        _LOG.warning("Artist section %s unavailable (%s)", section, type(exc).__name__)
        return []


def _persist_artist_details(server: Any, payload: Dict[str, Any], cache_key: str = "") -> None:
    artist_id = str(payload.get("id") or "").strip()
    if not artist_id or not artist_credit(payload.get("name")):
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
        thumbnail = str(record.get("thumbnail") or "")
        if cache_key and thumbnail.startswith("/artist_artwork/"):
            server.cache_store("artist", cache_key, {**payload, "thumbnail": thumbnail})

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
    wanted_provider = str(artist.get("provider_artist_id") or artist.get("id") or "").strip()
    stored_provider = str(persisted.get("provider_artist_id") or persisted.get("browseId") or "").strip()
    # Names alone are insufficient: homonymous artists must keep their own art.
    if not wanted_provider or wanted_provider != stored_provider:
        return artist
    thumbnail = str(persisted.get("thumbnail") or "").strip()
    if thumbnail.startswith("/artist_artwork/"):
        artist["thumbnail"] = thumbnail
    if persisted.get("canonical_artist_id"):
        artist["canonical_artist_id"] = persisted["canonical_artist_id"]
    return artist


def normalize_album_details(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Repair legacy detail shape at read/persistence boundaries, without provider work."""
    album = dict(payload)
    parent = artist_credit(album.get("artist"), album.get("artist_name"), album.get("channel"))
    tracks = []
    for raw in album.get("tracks") or []:
        if not isinstance(raw, dict):
            continue
        track = dict(raw)
        credit = artist_credit(track.get("artist"), track.get("channel"), track.get("artist_name"), parent)
        if credit:
            track.update(artist=credit, channel=credit)
        identity = str(track.get("id") or "")
        provider = track.get("source_provider") or track.get("provider")
        image = urlparse(str(track.get("thumbnail") or ""))
        match = re.search(r"/vi(?:_webp)?/([^/]+)/", image.path)
        if (not track.get("playback") and not track.get("videoId")
                and provider in {None, "youtube", "ytmusic"}
                and re.fullmatch(r"[A-Za-z0-9_-]{11}", identity)
                and image.hostname in {"i.ytimg.com", "img.youtube.com"}
                and match and match.group(1) == identity):
            track["videoId"] = identity
        tracks.append(normalized_track_payload(track))
    album["tracks"] = tracks
    if parent:
        album["artist"] = parent
    return album


def _persist_album_details(
    server: Any,
    cache_key: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    album = normalized_album_payload(normalize_album_details(dict(payload or {})))

    def persist(record: Dict[str, Any]) -> None:
        if cache_key:
            server.cache_store("album", cache_key, record)
        cache_albums([record])
        remember_catalog_entity(
            server,
            user_scope_id="global",
            query=str(record.get("title") or ""),
            entity_type="album",
            item=record,
            confidence=0.98,
            event_weight=0.0,
            event_type="album_detail_metadata",
            source="ytmusic_album_detail",
            learn_query_alias=False,
        )

    prepared = attach_cached_entity_artwork(
        server,
        album,
        entity_type="album",
    )
    try:
        persist(prepared)
        if not str(prepared.get("thumbnail") or "").startswith(
            "/entity_artwork/"
        ):
            schedule_entity_artwork_cache(
                server,
                prepared,
                entity_type="album",
                on_cached=persist,
            )
    except Exception:
        return prepared
    return prepared


def _stored_artist_identity(server, artist_id):
    from ..recommend.store_runtime import open_recommendation_store_connection

    provider_key = f"provider:artist:{artist_id.casefold()}"
    try:
        connection = open_recommendation_store_connection(server)
        try:
            row = connection.execute(
                "SELECT payload_json FROM catalog_entities "
                "WHERE entity_type='artist' AND entity_key IN (?, ?) "
                "ORDER BY confidence DESC LIMIT 1",
                (artist_id.casefold(), provider_key),
            ).fetchone()
        finally:
            connection.close()
        return json.loads(row[0]) if row else {}
    except Exception:
        return {}


def build_artist_details_payload(
    server: Any,
    artist_id: str,
    *,
    enrich_related: bool = True,
    lightweight: bool = False,
    section: str = "",
    collection_id: str = "",
    defer_persist=None,
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
            if section:
                return _artist_works(server, cached, section, collection_id)
            return dict(cached)

    provider_id = artist_id.removeprefix("ytmusic:artist:").removeprefix("MPLA")
    saved = {}
    if lightweight and artist_id.startswith(("musicbrainz:artist:", "artist-name:", "derived:")):
        saved = _stored_artist_identity(server, artist_id)
        provider_id = str(saved.get("provider_artist_id") or "").removeprefix("MPLA")
    creator_page = False
    try:
        try:
            raw_artist = server.ytmusic.get_artist(provider_id) if provider_id else {}
        except KeyError as exc:
            if exc.args != ("musicImmersiveHeaderRenderer",):
                raise
            raw_artist = server.ytmusic.get_user(provider_id)
            if not isinstance(raw_artist, dict) or not artist_credit(raw_artist.get("name")):
                raise
            creator_page = True
    except Exception:
        if not lightweight:
            raise
        saved = saved or _stored_artist_identity(server, provider_id)
        if not artist_credit(saved.get("name")):
            raise
        raw_artist = {}
    artist = dict(raw_artist) if isinstance(raw_artist, dict) else {}
    name = artist_credit(artist.get("name"), saved.get("name")) or "Unknown Artist"

    def artist_section(key):
        value = artist.get(key)
        return value if isinstance(value, dict) else {}

    songs_section = ({"results": artist_section("videos").get("results") or []}
                     if creator_page else artist_section("songs"))
    album_section = artist_section("albums")
    related_section = artist_section("related")

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
        "thumbnail": thumbnail or saved.get("thumbnail"),
        "stats": stats,
        "top_songs": top_songs[:12],
        "albums": albums[:12],
        "related_artists": related_artists[:12],
    }
    if lightweight:
        singles_section = artist_section("singles")
        raw_appearances = artist_section("appears_on").get("results")
        appearances = list(raw_appearances) if isinstance(raw_appearances, list) else []
        for song in songs_section.get("results") or []:
            if not isinstance(song, dict):
                continue
            album = song.get("album")
            credits = song.get("artists") or []
            if (isinstance(album, dict) and len(credits) > 1
                    and any(isinstance(credit, dict)
                            and str(credit.get("id") or "").removeprefix("MPLA") == provider_id
                            for credit in credits)
                    and isinstance(credits[0], dict) and credits[0].get("id")
                    and str(credits[0]["id"]).removeprefix("MPLA") != provider_id):
                appearances.append({**album, "artist": credits[0].get("name")})
        payload.update(
            provider_artist_id=provider_id,
            singles=_optional_artist_section("singles", lambda:
                server.normalize_artist_album_entries(singles_section.get("results") or [],
                                                      fallback_artist=name)),
            appears_on=_optional_artist_section("appears_on", lambda:
                server.normalize_artist_album_entries(appearances, fallback_artist=""))
                if appearances else [],
            featuring=_optional_artist_section("featuring", lambda: [
                {"id": item["playlistId"], "title": item["title"],
                 "thumbnail": server.extract_thumbnail(item)}
                for item in artist_section("playlists").get("results") or []
                if isinstance(item, dict) and item.get("playlistId") and item.get("title")
            ][:12]),
            radio_id=artist.get("radioId"),
            work_sections={
                key: {"browse_id": value["browseId"], "params": value.get("params")}
                for key, value in (("albums", album_section), ("singles", singles_section),
                                   ("songs", songs_section))
                if value.get("browseId")
            },
        )
    payload = _with_persisted_artist_artwork(server, payload)
    if cache_key and artist_credit(name):
        server.cache_store("artist", cache_key, payload)
    if artist:
        if defer_persist is None:
            _persist_artist_details(server, payload, cache_key)
        else:
            defer_persist(_persist_artist_details, server, payload, cache_key)
    return _artist_works(server, payload, section, collection_id) if section else payload


def _artist_works(server, overview, section, collection_id):
    descriptor = (overview.get("work_sections") or {}).get(section) or {}
    key = f"{overview['id']}:works:{section}:{collection_id}"
    cached = server.cache_lookup("artist", key)
    if cached is not None:
        return dict(cached)
    name = overview["name"]
    if section in {"albums", "singles"}:
        items = overview.get(section, [])
        if descriptor.get("params"):
            releases = server.ytmusic.get_artist_albums(
                descriptor["browse_id"], descriptor["params"], limit=None)
            expanded = server.normalize_artist_album_entries(releases, fallback_artist=name)
            items = list({item["id"]: item for item in [*items, *expanded]}.values())
    elif section == "songs":
        items = overview.get("top_songs", [])
        if descriptor:
            playlist = server.ytmusic.get_playlist(
                descriptor["browse_id"].removeprefix("VL"), limit=None)
            expanded = server.normalize_artist_song_entries(
                playlist.get("tracks") or [], fallback_artist=name)
            # Preserve the provider's popularity order and retain any playable
            # preview entries that did not appear in its expanded playlist.
            catalog = {item["id"]: item for item in expanded}
            for item in items:
                catalog.setdefault(item["id"], item)
            items = list(catalog.values())
        if not items and artist_credit(name) and overview.get("provider_artist_id"):
            provider_id = overview["provider_artist_id"]
            # A similarly named channel or the artist's band is not a credit.
            results = server.ytmusic.search(name, filter="songs", limit=20)
            items = server.normalize_artist_song_entries([
                item for item in results
                if any(isinstance(credit, dict)
                       and str(credit.get("id") or "").removeprefix("MPLA") == provider_id
                       for credit in item.get("artists") or [])
            ], fallback_artist=name)
    elif section == "song_counts":
        requested = set(collection_id.split(",", 5)[:5])
        preview = overview.get("top_songs") or (
            server.cache_lookup("artist", f"{overview['id']}:works:songs:") or {}
        ).get("items", [])
        items = []
        for track in preview[:12]:
            source = track.get("videoId")
            if not source or source not in requested:
                continue
            count = server.cache_lookup("track", f"{source}:views")
            if count is None:
                try:
                    details = server.ytmusic.get_song(source).get("videoDetails") or {}
                    if details.get("videoId") != source:
                        continue
                    count = int(details["viewCount"])
                except (KeyError, TypeError, ValueError):
                    continue
                except Exception as exc:
                    _LOG.warning("Artist song count unavailable (%s)", type(exc).__name__)
                    continue
                server.cache_store("track", f"{source}:views", count)
            items.append({"id": source, "views": count})
        # Cache individual successful values, not a partial batch.
        return {"status": "success", "items": items}
    elif section == "radio" and overview.get("radio_id"):
        radio = server.ytmusic.get_watch_playlist(playlistId=overview["radio_id"], limit=25)
        items = server.normalize_artist_song_entries(
            radio.get("tracks") or [], fallback_artist="")
    elif section == "featuring" and any(
        item["id"] == collection_id for item in overview.get("featuring", [])
    ):
        playlist = server.ytmusic.get_playlist(collection_id, limit=100)
        items = server.normalize_artist_song_entries(
            playlist.get("tracks") or [], fallback_artist="")
    else:
        return {"status": "success", "items": []}
    result = {"status": "success", "items": items}
    if items:
        server.cache_store("artist", key, result)
    return result


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
    # Some watch responses omit videoDetails while their selected queue item
    # still carries authoritative metadata. Match the requested source only.
    selected_track = next((track for track in watch.get("tracks", [])
                           if track.get("videoId") == video_id), {})
    video_details = {**selected_track, **{key: value for key, value in video_details.items() if value}}
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
            "channel": artist_credit(server.extract_artist(entry), album_artist),
            "videoId": video_id,
            "album": album.get("title"),
            "album_title": album.get("title"),
            "album_id": album_id,
            "year": album.get("year") or "",
            "release_date": entry.get("releaseDate") or entry.get("release_date")
            or album.get("releaseDate") or album.get("release_date") or "",
            "views": entry.get("views"),
        })

    payload = {
        "status": "success",
        "id": album_id,
        "title": album.get("title"),
        "artist": album_artist,
        "thumbnail": album_thumbnail,
        "year": album.get("year") or "",
        "release_date": album.get("releaseDate") or album.get("release_date") or "",
        "track_count": len(tracks),
        "views": album.get("views"),
        "tracks": tracks,
    }
    return _persist_album_details(server, cache_key, payload)
