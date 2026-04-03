from __future__ import annotations

from typing import Dict, Iterable, List

from ..storage.postgres import upsert_catalog_rows


def cache_tracks(tracks: Iterable[Dict]) -> None:
    rows = []
    for track in tracks or []:
        track_id = track.get("id")
        if not track_id:
            continue
        rows.append(
            {
                "track_id": track_id,
                "title": track.get("title") or "",
                "artist_name": track.get("channel") or track.get("artist") or "",
                "album_title": track.get("album") or "",
                **dict(track),
            }
        )
    upsert_catalog_rows(table="catalog_tracks", key_name="track_id", rows=rows)


def cache_artists(artists: Iterable[Dict]) -> None:
    rows = []
    for artist in artists or []:
        artist_id = artist.get("id")
        if not artist_id:
            continue
        rows.append({"artist_id": artist_id, **dict(artist)})
    upsert_catalog_rows(table="catalog_artists", key_name="artist_id", rows=rows)


def cache_albums(albums: Iterable[Dict]) -> None:
    rows = []
    for album in albums or []:
        album_id = album.get("id")
        if not album_id:
            continue
        rows.append(
            {
                "album_id": album_id,
                "artist_name": album.get("artist") or "",
                **dict(album),
            }
        )
    upsert_catalog_rows(table="catalog_albums", key_name="album_id", rows=rows)


def cache_search_payload(
    *,
    tracks: List[Dict],
    artists: List[Dict],
    albums: List[Dict],
) -> None:
    cache_tracks(tracks)
    cache_artists(artists)
    cache_albums(albums)
