from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from copy import deepcopy
import re
import threading
import time
from types import SimpleNamespace
from typing import Any
from typing import Dict, List
import unicodedata

from ..domain.catalog import (
    cache_search_payload,
    catalog_thumbnail_url,
    normalize_album_title,
    normalize_artist_name,
    normalized_album_payload,
    normalized_artist_payload,
    normalized_popularity,
    verified_playback_source,
)
from ..domain.retrieval import (
    load_artist_entity_expansion,
    retrieve_search_candidates_fast,
)
from ..discovery.structured_providers import LastFmClient
from ..recommend.feature_store import request_store_runtime
from ..storage.postgres import load_catalog_artist_payloads
from .pipeline import (
    rank_album_candidates_fast_path,
    rank_artist_candidates_fast_path,
    rank_track_candidates_fast_path,
)
from .query_mode import resolve_search_mode
from .catalog_pipeline import (
    catalog_album_is_detail_ready,
    catalog_albums_for_artist,
    catalog_playable_tracks_for_artist,
)
from .runtime import (
    search_artists_direct_cached,
    search_playlists_direct,
    search_query_intent,
    semantic_search_suggestion_items,
)
from .server_adapter import SearchServerAdapter
from .intelligence import (
    catalog_entity_key,
    load_catalog_artist_records,
    remember_catalog_entity,
    remember_search_resolution,
    search_text_similarity,
)
from ..storage.artist_artwork import (
    attach_cached_artist_artwork,
    register_artist_metadata_listener,
    schedule_artist_artwork_cache,
)


_SEARCH_SNAPSHOT_TTL_SECONDS = 10 * 60
_SEARCH_SNAPSHOT_MAX_ENTRIES = 96
_SEARCH_SNAPSHOT_LOCK = threading.RLock()
_SEARCH_SNAPSHOTS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_SEARCH_CATALOG_WRITER = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="auralis-search-catalog",
)
_SEARCH_ARTIST_METADATA_WRITER = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="auralis-search-artist-metadata",
)
_SEARCH_ARTIST_METADATA_LOCK = threading.Lock()
_SEARCH_ARTIST_METADATA_PENDING: set[str] = set()


def _search_snapshot_key(user_scope_id: str, query: str, search_mode: str) -> str:
    return "||".join(
        [
            str(user_scope_id or "guest").strip() or "guest",
            "canonical-artist-search-v2",
            " ".join(str(query or "").strip().lower().split()),
            str(search_mode or "exact").strip().lower() or "exact",
        ]
    )


def _load_search_snapshot(key: str) -> Dict[str, Any] | None:
    now = time.time()
    with _SEARCH_SNAPSHOT_LOCK:
        expired = [
            snapshot_key
            for snapshot_key, snapshot in _SEARCH_SNAPSHOTS.items()
            if float(snapshot.get("expires_at") or 0.0) <= now
        ]
        for snapshot_key in expired:
            _SEARCH_SNAPSHOTS.pop(snapshot_key, None)
        snapshot = _SEARCH_SNAPSHOTS.get(key)
        if snapshot is None:
            return None
        _SEARCH_SNAPSHOTS.move_to_end(key)
        return deepcopy(snapshot)


def _store_search_snapshot(key: str, snapshot: Dict[str, Any]) -> None:
    stored = deepcopy(snapshot)
    stored["expires_at"] = time.time() + _SEARCH_SNAPSHOT_TTL_SECONDS
    with _SEARCH_SNAPSHOT_LOCK:
        _SEARCH_SNAPSHOTS[key] = stored
        _SEARCH_SNAPSHOTS.move_to_end(key)
        while len(_SEARCH_SNAPSHOTS) > _SEARCH_SNAPSHOT_MAX_ENTRIES:
            _SEARCH_SNAPSHOTS.popitem(last=False)


def _artist_provider_id(artist: Dict[str, Any]) -> str:
    provider_id = str(
        artist.get("provider_artist_id")
        or artist.get("browseId")
        or artist.get("artist_id")
        or artist.get("id")
        or ""
    ).strip()
    if not provider_id or provider_id.startswith(
        ("musicbrainz:artist:", "artist-name:", "derived:")
    ):
        return ""
    return provider_id.casefold()


def _artist_musicbrainz_id(artist: Dict[str, Any]) -> str:
    musicbrainz_id = str(
        artist.get("musicbrainz_artist_id")
        or artist.get("artist_mbid")
        or artist.get("mb_artist_id")
        or ""
    ).strip()
    if musicbrainz_id:
        return musicbrainz_id.casefold()
    canonical_id = str(
        artist.get("canonical_artist_id")
        or artist.get("canonical_artist_key")
        or ""
    ).strip().casefold()
    if canonical_id.startswith("musicbrainz:artist:"):
        return canonical_id.removeprefix("musicbrainz:artist:")
    return ""


def _artist_name_key(artist: Dict[str, Any]) -> str:
    return normalize_artist_name(
        artist.get("normalized_name")
        or artist.get("name")
        or artist.get("artist")
        or artist.get("channel")
    )


def _raw_artist_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"\s*-\s*topic$", "", text)
    text = re.sub(r"\s*vevo$", "", text)
    return " ".join(text.split())


def _artist_alias_keys(artist: Dict[str, Any]) -> set[str]:
    values: List[Any] = [
        artist.get("name"),
        artist.get("artist"),
        artist.get("artist_name"),
        artist.get("channel"),
    ]
    for key in ("artist_aliases", "aliases"):
        aliases = artist.get(key)
        if isinstance(aliases, list):
            values.extend(aliases)
    return {
        normalized
        for value in values
        if (normalized := normalize_artist_name(value))
    }


def _artist_item_matches(
    item: Dict[str, Any],
    artist: Dict[str, Any],
) -> bool:
    item_artist = {
        "id": (
            item.get("artist_id")
            or item.get("artistId")
            or item.get("artist_browse_id")
        ),
        "musicbrainz_artist_id": item.get("musicbrainz_artist_id"),
        "canonical_artist_id": item.get("canonical_artist_id"),
        "name": (
            item.get("artist")
            or item.get("artist_name")
            or item.get("channel")
            or item.get("author")
        ),
    }
    if _same_artist_identity(item_artist, artist):
        return True
    item_name = normalize_artist_name(item_artist.get("name"))
    return bool(item_name and item_name in _artist_alias_keys(artist))


def _merge_artist_values(
    base: Dict[str, Any],
    incoming: Dict[str, Any],
) -> Dict[str, Any]:
    base_tokens = _artist_identity_tokens(base)
    incoming_tokens = _artist_identity_tokens(incoming)
    if base_tokens and incoming_tokens and not (base_tokens & incoming_tokens):
        # Matching names are not enough to merge two resolved artists. This is
        # what previously allowed a later provider lookup to replace the
        # already-selected Dio/Nirvana identity with a homonymous artist.
        return dict(base)
    merged = {
        **base,
        **{
            key: value
            for key, value in incoming.items()
            if value not in (None, "", [], {})
        },
    }
    aliases: List[str] = []
    for payload in (base, incoming):
        for key in ("artist_aliases", "aliases"):
            values = payload.get(key)
            if not isinstance(values, list):
                continue
            for value in values:
                normalized = normalize_artist_name(value)
                if normalized and normalized not in aliases:
                    aliases.append(normalized)
    if aliases:
        merged["artist_aliases"] = aliases
    return merged


def _artist_identity_tokens(artist: Dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    musicbrainz_id = _artist_musicbrainz_id(artist)
    provider_id = _artist_provider_id(artist)
    if musicbrainz_id:
        tokens.add(f"mbid:{musicbrainz_id}")
    if provider_id:
        tokens.add(f"provider:{provider_id}")
    return tokens


def _artist_merge_key(artist: Dict[str, Any]) -> str:
    """Return an identity key without collapsing unrelated homonymous artists."""
    musicbrainz_id = _artist_musicbrainz_id(artist)
    if musicbrainz_id:
        return f"mbid:{musicbrainz_id}"
    provider_id = _artist_provider_id(artist)
    if provider_id:
        return f"provider:{provider_id}"
    name_key = _artist_name_key(artist)
    return f"name:{name_key}" if name_key else ""


def _same_artist_identity(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    left_tokens = _artist_identity_tokens(left)
    right_tokens = _artist_identity_tokens(right)
    if left_tokens and right_tokens:
        return bool(left_tokens & right_tokens)
    left_name = _artist_name_key(left)
    right_name = _artist_name_key(right)
    if not left_name or left_name != right_name:
        return False
    # A name may bridge an unresolved placeholder to one resolved entity, but it
    # must never bridge two different provider or MusicBrainz identities.
    return not left_tokens or not right_tokens


def _album_identity(album: Dict[str, Any]) -> str:
    return str(
        album.get("canonical_album_identity")
        or album.get("musicbrainz_release_group_id")
        or album.get("musicbrainz_release_id")
        or album.get("id")
        or (
            f"{album.get('title') or album.get('name') or ''}|"
            f"{album.get('artist') or album.get('artist_name') or ''}"
        )
    ).strip().casefold()


def _album_match_keys(item: Dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    album_value = item.get("album")
    album_mapping = album_value if isinstance(album_value, dict) else {}
    for value in (
        item.get("album_id"),
        item.get("albumId"),
        item.get("browseId"),
        album_mapping.get("id"),
        album_mapping.get("browseId"),
        item.get("id") if item.get("canonical_album_identity") else None,
    ):
        text = str(value or "").strip().casefold()
        if text:
            keys.add(f"id:{text}")
    title = normalize_album_title(
        item.get("title")
        if item.get("canonical_album_identity")
        else (
            album_mapping.get("title")
            or album_mapping.get("name")
            or item.get("album_title")
            or album_value
        )
    )
    artist = normalize_artist_name(
        item.get("artist")
        or item.get("artist_name")
        or item.get("channel")
    )
    if title:
        keys.add(f"title:{title}|{artist}")
        keys.add(f"title:{title}")
    return keys


def _repair_search_artwork(
    tracks: List[Dict[str, Any]],
    albums: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Share known canonical artwork without performing network lookups."""
    repaired_tracks: List[Dict[str, Any]] = []
    track_art_by_album: Dict[str, str] = {}
    for raw_track in tracks:
        track = dict(raw_track or {})
        thumbnail = catalog_thumbnail_url(track, entity_type="track")
        if thumbnail:
            track["thumbnail"] = thumbnail
            for key in _album_match_keys(track):
                track_art_by_album.setdefault(key, thumbnail)
        repaired_tracks.append(track)

    repaired_albums: List[Dict[str, Any]] = []
    album_art_by_key: Dict[str, str] = {}
    for raw_album in albums:
        album = normalized_album_payload(dict(raw_album or {}))
        thumbnail = catalog_thumbnail_url(album, entity_type="album")
        if not thumbnail:
            thumbnail = next(
                (
                    track_art_by_album[key]
                    for key in _album_match_keys(album)
                    if key in track_art_by_album
                ),
                "",
            )
        if thumbnail:
            album["thumbnail"] = thumbnail
            for key in _album_match_keys(album):
                album_art_by_key.setdefault(key, thumbnail)
        repaired_albums.append(album)

    for track in repaired_tracks:
        if catalog_thumbnail_url(track, entity_type="track"):
            continue
        thumbnail = next(
            (
                album_art_by_key[key]
                for key in _album_match_keys(track)
                if key in album_art_by_key
            ),
            "",
        )
        if thumbnail:
            track["thumbnail"] = thumbnail
    return repaired_tracks, repaired_albums


def _artist_catalog_albums(
    artists: List[Dict[str, Any]],
    *,
    relationship: str,
) -> List[Dict[str, Any]]:
    albums: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for artist in artists:
        artist_name = str(
            artist.get("name")
            or artist.get("artist")
            or artist.get("channel")
            or ""
        ).strip()
        related_to_artist = _artist_merge_key(artist)
        artist_aliases = _artist_alias_keys(artist)
        for raw_album in list(artist.get("albums") or []):
            if not isinstance(raw_album, dict):
                continue
            album = normalized_album_payload(
                {
                    **raw_album,
                    "artist": raw_album.get("artist") or artist_name,
                    "related_to_artist": related_to_artist,
                    "relationship_evidence": relationship,
                }
            )
            if (
                not album.get("title")
                or normalize_artist_name(album.get("artist") or "")
                not in artist_aliases
            ):
                continue
            key = _album_identity(album)
            if not key or key in seen:
                continue
            seen.add(key)
            albums.append(album)
    return albums


def _update_search_snapshots_artist(artist: Dict[str, Any]) -> None:
    if not _artist_merge_key(artist):
        return
    with _SEARCH_SNAPSHOT_LOCK:
        for snapshot in _SEARCH_SNAPSHOTS.values():
            for surface in ("artists", "related_artists"):
                values = list(snapshot.get(surface) or [])
                for index, value in enumerate(values):
                    if (
                        not isinstance(value, dict)
                        or not _same_artist_identity(value, artist)
                    ):
                        continue
                    values[index] = {
                        **value,
                        **{
                            key: item
                            for key, item in artist.items()
                            if item not in (None, "", [], {})
                        },
                    }
                snapshot[surface] = values
            lead_artist = snapshot.get("lead_artist")
            if (
                isinstance(lead_artist, dict)
                and _same_artist_identity(lead_artist, artist)
            ):
                snapshot["lead_artist"] = {
                    **lead_artist,
                    **{
                        key: item
                        for key, item in artist.items()
                        if item not in (None, "", [], {})
                    },
                }


register_artist_metadata_listener(_update_search_snapshots_artist)


def _best_provider_artist_match(
    *,
    server: Any,
    artist_name: str,
) -> Dict[str, Any] | None:
    try:
        matches = search_artists_direct_cached(
            artist_name,
            6,
            server=server,
        )
    except Exception:
        matches = []
    ranked_matches = sorted(
        (
            dict(match)
            for match in matches
            if isinstance(match, dict)
            and str(match.get("id") or "").strip()
        ),
        key=lambda match: (
            search_text_similarity(
                artist_name,
                str(match.get("name") or match.get("artist") or ""),
            ),
            normalized_popularity(match),
            bool(str(match.get("thumbnail") or "").strip()),
        ),
        reverse=True,
    )
    best_match = ranked_matches[0] if ranked_matches else {}
    if best_match and search_text_similarity(
        artist_name,
        str(best_match.get("name") or best_match.get("artist") or ""),
    ) >= 0.78:
        return best_match
    return None


def _persist_search_artist(
    *,
    server: Any,
    query: str,
    artist: Dict[str, Any],
) -> None:
    try:
        cache_search_payload(tracks=[], artists=[artist], albums=[])
    except Exception:
        # The local canonical catalog is the runtime source of truth. A
        # database cache outage must not prevent artwork/identity persistence.
        pass
    remember_catalog_entity(
        server,
        user_scope_id="global",
        query=str(artist.get("name") or query),
        entity_type="artist",
        item=artist,
        confidence=0.88,
        event_weight=0.0,
        event_type="artist_metadata",
        source="canonical_search_artist",
    )
    _update_search_snapshots_artist(artist)


def _resolve_artist_metadata_background(
    *,
    server: Any,
    query: str,
    artist: Dict[str, Any],
) -> Dict[str, Any] | None:
    artist = dict(artist or {})
    artist_id = str(
        artist.get("provider_artist_id")
        or artist.get("id")
        or ""
    ).strip()
    artist_name = str(
        artist.get("name")
        or artist.get("artist")
        or artist.get("channel")
        or ""
    ).strip()
    artist_key = artist_id or _artist_merge_key(artist)
    if not artist_key:
        return None
    pending_key = artist_key.casefold()
    with _SEARCH_ARTIST_METADATA_LOCK:
        if pending_key in _SEARCH_ARTIST_METADATA_PENDING:
            return None
        _SEARCH_ARTIST_METADATA_PENDING.add(pending_key)
    try:
        cached_artist = attach_cached_artist_artwork(server, artist)
        if str(cached_artist.get("thumbnail") or "").startswith(
            "/artist_artwork/"
        ):
            _persist_search_artist(
                server=server,
                query=query,
                artist=cached_artist,
            )
            return cached_artist
        provider_id_is_usable = bool(artist_id) and not artist_id.startswith(
            ("musicbrainz:artist:", "artist-name:", "derived:")
        )
        if not provider_id_is_usable and artist_name:
            best_match = _best_provider_artist_match(
                server=server,
                artist_name=artist_name,
            )
            if best_match:
                original_canonical_id = str(
                    artist.get("canonical_artist_id")
                    or artist.get("canonical_artist_key")
                    or ""
                ).strip()
                artist = {
                    **artist,
                    **{
                        key: value
                        for key, value in best_match.items()
                        if value not in (None, "", [], {})
                    },
                }
                if original_canonical_id:
                    artist["canonical_artist_id"] = original_canonical_id
                artist_id = str(best_match.get("id") or "").strip()
        try:
            details = dict(
                SearchServerAdapter(server).build_artist_details_payload(
                    artist_id,
                    enrich_related=False,
                    lightweight=True,
                )
                or {}
            )
        except Exception:
            details = {}
        if not details and not str(artist.get("thumbnail") or "").strip():
            return None
        resolved = normalized_artist_payload(
            {
                **artist,
                **{
                    key: value
                    for key, value in {
                        "id": details.get("id") or artist_id,
                        "name": details.get("name") or artist_name,
                        "thumbnail": details.get("thumbnail"),
                        "description": details.get("description"),
                        "stats": details.get("stats"),
                        "albums": details.get("albums"),
                        "top_songs": details.get("top_songs"),
                        "source_authority": "ytmusic_artist_detail",
                    }.items()
                    if value not in (None, "", [], {})
                },
            }
        )
        _persist_search_artist(server=server, query=query, artist=resolved)
        schedule_artist_artwork_cache(
            server,
            resolved,
            on_cached=lambda cached: _persist_search_artist(
                server=server,
                query=query,
                artist=cached,
            ),
        )
        return resolved
    finally:
        with _SEARCH_ARTIST_METADATA_LOCK:
            _SEARCH_ARTIST_METADATA_PENDING.discard(pending_key)


def _cache_search_payload_background(
    *,
    server: Any,
    query: str,
    tracks: List[Dict[str, Any]],
    artists: List[Dict[str, Any]],
    albums: List[Dict[str, Any]],
) -> None:
    try:
        cache_search_payload(
            tracks=tracks,
            artists=artists,
            albums=albums,
        )
    except Exception:
        pass

    # Persist the accepted result set in the existing canonical catalog. The
    # next query can then build the same complete surfaces without repeating
    # provider work, even after the backend process restarts.
    for entity_type, values, confidence, cap in (
        ("track", tracks, 0.84, 96),
        ("album", albums, 0.84, 48),
    ):
        seen: set[str] = set()
        for raw_item in values[:cap]:
            item = dict(raw_item or {})
            key = catalog_entity_key(entity_type, item, query=query)
            if not key or key in seen:
                continue
            seen.add(key)
            remember_catalog_entity(
                server,
                user_scope_id="global",
                query=query,
                entity_type=entity_type,
                item=item,
                confidence=confidence,
                event_weight=0.0,
                event_type="search_catalog_hydration",
                source="canonical_search_result",
                learn_query_alias=False,
            )

    for raw_artist in artists:
        artist = normalized_artist_payload(raw_artist)
        _persist_search_artist(server=server, query=query, artist=artist)
        scheduled = schedule_artist_artwork_cache(
            server,
            artist,
            on_cached=lambda cached, search_query=query: _persist_search_artist(
                server=server,
                query=search_query,
                artist=cached,
            ),
        )
        if (
            not scheduled
            and not str(artist.get("thumbnail") or "").strip()
            and str(
                artist.get("provider_artist_id")
                or artist.get("id")
                or ""
            ).strip()
        ):
            _SEARCH_ARTIST_METADATA_WRITER.submit(
                _resolve_artist_metadata_background,
                server=server,
                query=query,
                artist=artist,
            )


class SearchService:
    def __init__(self, server: Any) -> None:
        self._server = server

    def _search_server(self) -> SearchServerAdapter:
        return SearchServerAdapter(self._server)

    def _lastfm_related_artists(
        self,
        lead_artist: Dict[str, Any],
        *,
        limit: int = 16,
    ) -> List[Dict[str, Any]]:
        name = str(
            lead_artist.get("name")
            or lead_artist.get("artist")
            or lead_artist.get("channel")
            or ""
        ).strip()
        if not name:
            return []
        musicbrainz_artist_id = str(
            lead_artist.get("musicbrainz_artist_id")
            or lead_artist.get("mbid")
            or ""
        ).strip()
        canonical_id = str(
            lead_artist.get("canonical_artist_id") or ""
        ).strip()
        if (
            not musicbrainz_artist_id
            and canonical_id.startswith("musicbrainz:artist:")
        ):
            musicbrainz_artist_id = canonical_id.removeprefix(
                "musicbrainz:artist:"
            )
        rows = LastFmClient(
            self._server,
            timeout_seconds=1.5,
        ).similar_artists(
            name,
            artist_mbid=musicbrainz_artist_id,
            limit=max(8, min(int(limit or 16), 24)),
        )
        lead_key = normalize_artist_name(name)
        output: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            artist = normalized_artist_payload(raw)
            name_key = _artist_name_key(artist)
            musicbrainz_id = _artist_musicbrainz_id(artist)
            key = (
                f"mbid:{musicbrainz_id}"
                if musicbrainz_id
                else name_key or _artist_merge_key(artist)
            )
            if (
                not key
                or name_key == lead_key
                or _same_artist_identity(lead_artist, artist)
                or key in seen
            ):
                continue
            seen.add(key)
            output.append(artist)
        return output

    def _resolve_first_page_related_artists(
        self,
        artists: List[Dict[str, Any]],
        *,
        query: str,
        limit: int = 6,
    ) -> List[Dict[str, Any]]:
        """Return cached artwork now and resolve missing artists off-path."""
        candidates = self._merge_snapshot_items(
            "related_artists",
            [],
            self._hydrate_artist_artwork(
                list(artists or []),
                allow_live_lead_lookup=False,
                schedule_background=False,
            ),
        )
        missing = [
            artist
            for artist in candidates
            if not str(artist.get("thumbnail") or "").strip()
        ][: max(int(limit or 0), 0)]
        if candidates:
            _SEARCH_CATALOG_WRITER.submit(
                _cache_search_payload_background,
                server=self._server,
                query=query,
                tracks=[],
                artists=candidates,
                albums=[],
            )
        for artist in missing:
            _SEARCH_ARTIST_METADATA_WRITER.submit(
                _resolve_artist_metadata_background,
                server=self._server,
                query=query,
                artist=artist,
            )
        return candidates

    @staticmethod
    def _surface_page(
        items: List[Dict[str, Any]],
        *,
        offset: int,
        limit: int,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        safe_offset = max(int(offset or 0), 0)
        safe_limit = max(int(limit or 1), 1)
        page = list(items[safe_offset : safe_offset + safe_limit])
        next_offset = safe_offset + len(page)
        return page, {
            "offset": safe_offset,
            "page_size": safe_limit,
            "next_offset": next_offset,
            "has_more": next_offset < len(items),
            "available": len(items),
        }

    def _hydrate_artist_artwork(
        self,
        artists: List[Dict[str, Any]],
        *,
        allow_live_lead_lookup: bool,
        schedule_background: bool = True,
    ) -> List[Dict[str, Any]]:
        hydrated: List[Dict[str, Any]] = []
        persisted_by_id = load_catalog_artist_payloads(
            str(artist.get("id") or "").strip()
            for artist in artists
            if isinstance(artist, dict)
        )
        persisted_by_name = load_catalog_artist_records(
            self._server,
            artist_names=(
                str(
                    artist.get("name")
                    or artist.get("artist")
                    or artist.get("channel")
                    or ""
                )
                for artist in artists
                if isinstance(artist, dict)
            ),
        )
        for index, raw_artist in enumerate(artists):
            artist = dict(raw_artist or {})
            artist_id = str(artist.get("id") or "").strip()
            has_stable_identity = bool(_artist_identity_tokens(artist))
            normalized_name = normalize_artist_name(
                artist.get("name")
                or artist.get("artist")
                or artist.get("channel")
            )
            persisted_name_match = persisted_by_name.get(normalized_name) or {}
            persisted = (
                persisted_by_id.get(artist_id)
                or (
                    persisted_name_match
                    if (
                        not has_stable_identity
                        or _same_artist_identity(artist, persisted_name_match)
                    )
                    else None
                )
                or {}
            )
            persisted_thumbnail = str(persisted.get("thumbnail") or "").strip()
            current_thumbnail = str(artist.get("thumbnail") or "").strip()
            if (
                persisted_thumbnail.startswith("/artist_artwork/")
                or not current_thumbnail
            ):
                artist["thumbnail"] = persisted_thumbnail or current_thumbnail
            for key in (
                "canonical_artist_id",
                "artist_aliases",
                "description",
                "subscribers",
                "stats",
                "source_authority",
                "albums",
            ):
                if not artist.get(key) and persisted.get(key):
                    artist[key] = persisted[key]
            persisted_id = str(
                persisted.get("provider_artist_id")
                or persisted.get("id")
                or ""
            ).strip()
            if persisted_id and (
                not artist_id
                or str(artist.get("resolution_status") or "").startswith("derived")
            ):
                artist["id"] = persisted_id
                artist_id = persisted_id
            if not artist.get("thumbnail") and index == 0:
                cached_artist = attach_cached_artist_artwork(
                    self._server,
                    artist,
                )
                if str(cached_artist.get("thumbnail") or "").startswith(
                    "/artist_artwork/"
                ):
                    artist = cached_artist
                    _SEARCH_CATALOG_WRITER.submit(
                        _persist_search_artist,
                        server=self._server,
                        query=str(artist.get("name") or ""),
                        artist=artist,
                    )
            if (
                not artist.get("thumbnail")
                and artist_id
                and allow_live_lead_lookup
                and index == 0
            ):
                try:
                    details = self._search_server().build_artist_details_payload(
                        artist_id,
                        enrich_related=False,
                        lightweight=True,
                    )
                except Exception:
                    details = {}
                if details:
                    artist.update(
                        {
                            key: value
                            for key, value in {
                                "id": details.get("id") or artist_id,
                                "name": details.get("name"),
                                "thumbnail": details.get("thumbnail"),
                        "description": details.get("description"),
                        "stats": details.get("stats"),
                        "albums": details.get("albums"),
                        "source_authority": "ytmusic_artist_detail",
                            }.items()
                            if value not in (None, "")
                        }
                    )
            hydrated.append(artist)
        if hydrated and schedule_background:
            _SEARCH_CATALOG_WRITER.submit(
                _cache_search_payload_background,
                server=self._server,
                query=str(
                    next(
                        (
                            artist.get("name")
                            for artist in hydrated
                            if artist.get("name")
                        ),
                        "",
                    )
                ),
                tracks=[],
                artists=hydrated,
                albums=[],
            )
        return hydrated

    def _rehydrate_search_snapshot(
        self,
        snapshot: Dict[str, Any],
        *,
        schedule_background: bool = False,
    ) -> Dict[str, Any]:
        refreshed = dict(snapshot)
        for surface in ("artists", "related_artists"):
            hydrated = self._hydrate_artist_artwork(
                list(refreshed.get(surface) or []),
                allow_live_lead_lookup=False,
                schedule_background=schedule_background,
            )
            refreshed[surface] = self._merge_snapshot_items(
                surface,
                [],
                hydrated,
            )
        lead_artist = refreshed.get("lead_artist")
        if isinstance(lead_artist, dict) and lead_artist:
            hydrated_lead = self._hydrate_artist_artwork(
                [lead_artist],
                allow_live_lead_lookup=False,
                schedule_background=schedule_background,
            )
            if hydrated_lead:
                refreshed["lead_artist"] = hydrated_lead[0]
                lead = hydrated_lead[0]
                refreshed["artists"] = [
                    (
                        _merge_artist_values(artist, lead)
                        if _same_artist_identity(artist, lead)
                        or _raw_artist_name(artist.get("name"))
                        == _raw_artist_name(lead.get("name"))
                        else artist
                    )
                    for artist in list(refreshed.get("artists") or [])
                ]
        hydrated_lead_artist = refreshed.get("lead_artist")
        if isinstance(hydrated_lead_artist, dict):
            refreshed["artist_albums"] = self._merge_snapshot_items(
                "albums",
                list(refreshed.get("artist_albums") or []),
                _artist_catalog_albums(
                    [hydrated_lead_artist],
                    relationship="lead_artist_discography",
                ),
            )
        refreshed["related_albums"] = self._merge_snapshot_items(
            "albums",
            list(refreshed.get("related_albums") or []),
            _artist_catalog_albums(
                list(refreshed.get("related_artists") or []),
                relationship="related_artist_discography",
            ),
        )
        return refreshed

    @staticmethod
    def _missing_artist_artwork(snapshot: Dict[str, Any]) -> int:
        unique_missing: set[str] = set()
        artists = [
            *list(snapshot.get("artists") or []),
            *list(snapshot.get("related_artists") or []),
        ]
        lead_artist = snapshot.get("lead_artist")
        if isinstance(lead_artist, dict):
            artists.append(lead_artist)
        for artist in artists:
            if not isinstance(artist, dict):
                continue
            if str(artist.get("thumbnail") or "").strip():
                continue
            key = _artist_merge_key(artist)
            if key:
                unique_missing.add(key)
        return len(unique_missing)

    def _lead_entities(
        self,
        *,
        query: str,
        tracks: List[Dict[str, Any]],
        artists: List[Dict[str, Any]],
        albums: List[Dict[str, Any]],
        query_intent: str,
        resolved_artist: Dict[str, Any] | None = None,
    ) -> tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
        search = self._search_server()
        resolved = dict(resolved_artist or {})
        lead_artist: Dict[str, Any] | None = (
            resolved if resolved else artists[0] if artists else None
        )
        containing_album: Dict[str, Any] | None = albums[0] if albums else None
        if tracks:
            track = tracks[0]
            if resolved and query_intent in {"track", "mixed"}:
                track = next(
                    (
                        candidate
                        for candidate in tracks
                        if _artist_item_matches(candidate, resolved)
                    ),
                    track,
                )
            credited_name = str(
                track.get("channel") or track.get("artist") or ""
            ).strip()
            credited_id = str(
                track.get("artist_id")
                or track.get("artistId")
                or track.get("artist_browse_id")
                or ""
            ).strip()
            if credited_name:
                matching_artist = None
                if credited_id:
                    matching_artist = next(
                        (
                            item
                            for item in artists
                            if str(
                                item.get("provider_artist_id")
                                or item.get("id")
                                or ""
                            ).strip()
                            == credited_id
                        ),
                        None,
                    )
                if matching_artist is None:
                    matching_artist = max(
                        (
                            item
                            for item in artists
                            if search.normalize_text(item.get("name"))
                            == search.normalize_text(credited_name)
                        ),
                        key=normalized_popularity,
                        default=None,
                    )
                credited_artist = dict(
                    matching_artist
                    or {"id": credited_id, "name": credited_name}
                )
                if (
                    resolved
                    and query_intent in {"track", "mixed"}
                    and not _same_artist_identity(resolved, credited_artist)
                    and normalize_artist_name(credited_name)
                    not in _artist_alias_keys(resolved)
                ):
                    lead_artist = resolved
                else:
                    lead_artist = (
                        _merge_artist_values(resolved, credited_artist)
                        if resolved
                        else credited_artist
                    )
            album_id = str(
                track.get("album_id") or track.get("albumId") or ""
            ).strip()
            album_title = str(
                track.get("album") or track.get("album_title") or ""
            ).strip()
            if album_title:
                matching_album = next(
                    (
                        item
                        for item in albums
                        if (
                            album_id
                            and str(item.get("id") or "").strip() == album_id
                        )
                        or search.normalize_text(item.get("title"))
                        == search.normalize_text(album_title)
                    ),
                    None,
                )
                containing_album = dict(
                    matching_album
                    or {
                        "id": album_id,
                        "title": album_title,
                        "artist": credited_name,
                        "thumbnail": track.get("thumbnail"),
                    }
                )
        if query_intent == "artist" and artists:
            raw_query = _raw_artist_name(query)
            credited_names: Dict[str, int] = {}
            credited_ids: Dict[str, int] = {}
            for track in tracks[:24]:
                credited_name = normalize_artist_name(
                    track.get("channel")
                    or track.get("artist")
                    or track.get("artist_name")
                )
                credited_id = str(
                    track.get("artist_id")
                    or track.get("artistId")
                    or track.get("artist_browse_id")
                    or ""
                ).strip().casefold()
                if credited_name:
                    credited_names[credited_name] = (
                        credited_names.get(credited_name, 0) + 1
                    )
                if credited_id:
                    credited_ids[credited_id] = (
                        credited_ids.get(credited_id, 0) + 1
                    )

            def score(artist: Dict[str, Any]) -> tuple[float, float, str]:
                name = str(
                    artist.get("name")
                    or artist.get("artist")
                    or ""
                ).strip()
                name_key = normalize_artist_name(name)
                provider_id = _artist_provider_id(artist)
                exact_raw = _raw_artist_name(name) == raw_query
                resolved_match = bool(
                    resolved
                    and (
                        _same_artist_identity(artist, resolved)
                        or _raw_artist_name(name)
                        == _raw_artist_name(
                            resolved.get("name")
                            or resolved.get("artist")
                        )
                    )
                )
                relationship_support = (
                    credited_names.get(name_key, 0) * 1.8
                    + credited_ids.get(provider_id, 0) * 2.2
                )
                source_authority = str(
                    artist.get("source_authority") or ""
                ).strip().lower()
                authority_score = (
                    2.0
                    if source_authority
                    in {
                        "official",
                        "official_artist_channel",
                        "verified_catalog",
                        "ytmusic_artist_detail",
                    }
                    else 0.0
                )
                return (
                    (18.0 if exact_raw else 0.0)
                    + (8.0 if resolved_match else 0.0)
                    + relationship_support
                    + authority_score
                    + normalized_popularity(artist) * 2.0
                    + search_text_similarity(query, name) * 5.0,
                    relationship_support,
                    name,
                )

            lead_artist = dict(max(artists, key=score))
        elif query_intent == "album" and albums:
            containing_album = albums[0]
        return lead_artist, containing_album

    def _complete_artist_search_surfaces(
        self,
        *,
        query: str,
        user_scope_id: str,
        lead_artist: Dict[str, Any],
        tracks: List[Dict[str, Any]],
        albums: List[Dict[str, Any]],
        related_artists: List[Dict[str, Any]],
        playlists: List[Dict[str, Any]],
        limit: int,
    ) -> Dict[str, Any]:
        """Build every artist-query surface from one canonical artist."""
        artist_name = str(
            lead_artist.get("name")
            or lead_artist.get("artist")
            or ""
        ).strip()
        artist_id = str(
            lead_artist.get("provider_artist_id")
            or lead_artist.get("id")
            or ""
        ).strip()

        local_started_at = time.perf_counter()
        persisted_artist = load_catalog_artist_records(
            self._server,
            artist_names=[artist_name],
        ).get(normalize_artist_name(artist_name), {})
        if persisted_artist:
            lead_artist = _merge_artist_values(lead_artist, persisted_artist)
            artist_id = str(
                lead_artist.get("provider_artist_id")
                or lead_artist.get("id")
                or artist_id
            ).strip()
        local_tracks = catalog_playable_tracks_for_artist(
            self._server,
            user_scope_id=user_scope_id or "guest",
            artist=artist_name,
            limit=max(int(limit or 0), 48),
        )
        local_albums = catalog_albums_for_artist(
            self._server,
            artist=artist_name,
            limit=max(min(int(limit or 0), 24), 12),
        )
        canonical_tracks = self._merge_snapshot_items(
            "tracks",
            [],
            [
                dict(item)
                for item in [*local_tracks, *tracks]
                if isinstance(item, dict)
                and _artist_item_matches(item, lead_artist)
            ],
        )
        canonical_albums = self._merge_snapshot_items(
            "albums",
            [],
            [
                dict(item)
                for item in [*local_albums, *albums]
                if isinstance(item, dict)
                and _artist_item_matches(item, lead_artist)
            ],
        )
        canonical_tracks, canonical_albums = _repair_search_artwork(
            canonical_tracks,
            canonical_albums,
        )
        local_catalog_ms = int((time.perf_counter() - local_started_at) * 1000)
        local_catalog_complete = (
            len(canonical_tracks) >= 20
            and len(canonical_albums) >= 4
        )

        # Last.fm relationship lookup does not depend on album-tracklist
        # completion. Start it now so unseen artist completion does not pay the
        # two provider latencies one after the other.
        lastfm_started_at = time.perf_counter()
        lastfm_future = None
        search_executor = getattr(self._server, "search_executor", None)
        if len(related_artists) < 8 and search_executor is not None:
            try:
                lastfm_future = search_executor.submit(
                    self._lastfm_related_artists,
                    lead_artist,
                    limit=16,
                )
            except Exception:
                lastfm_future = None

        catalog_source = "local_canonical"
        live_catalog_ms = 0
        catalog: Dict[str, Any] = {
            "artist": persisted_artist,
            "tracks": canonical_tracks,
            "albums": canonical_albums,
            "related_artists": [],
            "catalog_status": "complete",
            "album_tracklists_loaded": 0,
        }
        if not local_catalog_complete:
            live_started_at = time.perf_counter()
            live_catalog = load_artist_entity_expansion(
                self._server,
                artist_id=artist_id,
                artist_name=artist_name,
                user_scope_id=user_scope_id or "guest",
                limit=max(int(limit or 0), 48),
                include_related=False,
            )
            live_catalog_ms = int(
                (time.perf_counter() - live_started_at) * 1000
            )
            catalog_source = "live_completion"
            catalog = dict(live_catalog or {})
            canonical_tracks = self._merge_snapshot_items(
                "tracks",
                canonical_tracks,
                list(catalog.get("tracks") or []),
            )
            canonical_albums = self._merge_snapshot_items(
                "albums",
                canonical_albums,
                list(catalog.get("albums") or []),
            )
            catalog["tracks"] = canonical_tracks
            catalog["albums"] = canonical_albums

        catalog_artist = dict(catalog.get("artist") or {})
        if catalog_artist:
            lead_artist = _merge_artist_values(lead_artist, catalog_artist)
        hydrated_lead = self._hydrate_artist_artwork(
            [lead_artist],
            allow_live_lead_lookup=False,
            schedule_background=False,
        )
        if hydrated_lead:
            lead_artist = hydrated_lead[0]
        if not str(lead_artist.get("thumbnail") or "").strip():
            _SEARCH_ARTIST_METADATA_WRITER.submit(
                _resolve_artist_metadata_background,
                server=self._server,
                query=query,
                artist=lead_artist,
            )

        canonical_tracks = self._merge_snapshot_items(
            "tracks",
            [],
            [
                dict(item)
                for item in canonical_tracks
                if isinstance(item, dict)
                and bool(verified_playback_source(item))
                and _artist_item_matches(item, lead_artist)
            ],
        )
        canonical_albums = self._merge_snapshot_items(
            "albums",
            [],
            [
                dict(item)
                for item in canonical_albums
                if isinstance(item, dict)
                and _artist_item_matches(item, lead_artist)
            ],
        )
        canonical_tracks, canonical_albums = _repair_search_artwork(
            canonical_tracks,
            canonical_albums,
        )

        try:
            if lastfm_future is not None:
                lastfm_related = lastfm_future.result(timeout=2.0)
                related_status = "complete"
            elif len(related_artists) >= 8:
                lastfm_related = []
                related_status = "complete"
            else:
                lastfm_related = self._lastfm_related_artists(
                    lead_artist,
                    limit=16,
                )
                related_status = "complete"
        except FutureTimeoutError:
            lastfm_related = []
            related_status = "pending"
        except Exception:
            lastfm_related = []
            related_status = "retryable"
        lastfm_ms = int((time.perf_counter() - lastfm_started_at) * 1000)

        raw_related = self._merge_snapshot_items(
            "related_artists",
            [],
            [
                *lastfm_related,
                *list(catalog.get("related_artists") or []),
                *related_artists,
            ],
        )
        artwork_started_at = time.perf_counter()
        resolved_related = self._resolve_first_page_related_artists(
            raw_related,
            query=query,
            limit=8,
        )
        related_artwork_ms = int(
            (time.perf_counter() - artwork_started_at) * 1000
        )
        # An unresolved placeholder is not a completed artist card. Keep it
        # out of the visible shelf; its canonical identity remains persisted
        # and can be retried on the next snapshot build.
        resolved_related = [
            artist
            for artist in resolved_related
            if str(artist.get("thumbnail") or "").strip()
            and not _same_artist_identity(artist, lead_artist)
        ]
        resolved_related = self._merge_snapshot_items(
            "related_artists",
            [],
            resolved_related,
        )
        if raw_related and len(resolved_related) < min(6, len(raw_related)):
            related_status = "retryable"

        playlist_candidates = list(playlists or [])
        lead_keys = {
            normalize_artist_name(artist_name),
            *{
                normalize_artist_name(alias)
                for alias in list(lead_artist.get("artist_aliases") or [])
            },
        }
        lead_keys.discard("")
        related_keys = {
            normalize_artist_name(
                artist.get("name") or artist.get("artist")
            )
            for artist in resolved_related[:8]
        }
        related_keys.discard("")

        def playlist_is_relevant(playlist: Dict[str, Any]) -> bool:
            text = normalize_artist_name(
                " ".join(
                    str(value or "")
                    for value in (
                        playlist.get("name"),
                        playlist.get("title"),
                        playlist.get("author"),
                        playlist.get("subtitle"),
                    )
                )
            )
            if not text:
                return False

            def contains_artist_key(key: str) -> bool:
                key_tokens = key.split()
                text_tokens = set(text.split())
                if len(key_tokens) == 1:
                    return key_tokens[0] in text_tokens
                return key in text

            if any(contains_artist_key(key) for key in lead_keys):
                return True
            return any(contains_artist_key(key) for key in related_keys)

        relevant_playlists = self._merge_snapshot_items(
            "playlists",
            [],
            [
                dict(item)
                for item in playlist_candidates
                if isinstance(item, dict) and playlist_is_relevant(item)
            ],
        )
        provider_playlists_complete = len(relevant_playlists) >= 4
        if len(relevant_playlists) < 4 and canonical_tracks:
            stable_artist_key = (
                _artist_provider_id(lead_artist)
                or normalize_artist_name(artist_name).replace(" ", "-")
                or "artist"
            )
            generated_playlists: List[Dict[str, Any]] = []
            essentials = canonical_tracks[:32]
            if len(essentials) >= 4:
                generated_playlists.append(
                    {
                        "id": f"search-generated:{stable_artist_key}:essentials",
                        "name": f"{artist_name} Essentials",
                        "title": f"{artist_name} Essentials",
                        "subtitle": f"Canonical works by {artist_name}.",
                        "provider": "neatie",
                        "generated": True,
                        "track_count": len(essentials),
                        "tracks": essentials,
                        "thumbnail": (
                            lead_artist.get("thumbnail")
                            or essentials[0].get("thumbnail")
                        ),
                    }
                )
            deeper = canonical_tracks[8:40]
            if len(deeper) >= 4:
                generated_playlists.append(
                    {
                        "id": f"search-generated:{stable_artist_key}:catalog",
                        "name": f"More from {artist_name}",
                        "title": f"More from {artist_name}",
                        "subtitle": f"Deeper catalog works by {artist_name}.",
                        "provider": "neatie",
                        "generated": True,
                        "track_count": len(deeper),
                        "tracks": deeper,
                        "thumbnail": (
                            deeper[0].get("thumbnail")
                            or lead_artist.get("thumbnail")
                        ),
                    }
                )
            tracks_by_album: Dict[str, List[Dict[str, Any]]] = {}
            for track in canonical_tracks:
                album_title = str(
                    track.get("album")
                    or track.get("album_title")
                    or ""
                ).strip()
                if not album_title:
                    continue
                tracks_by_album.setdefault(album_title, []).append(track)
            for album_title, album_tracks in sorted(
                tracks_by_album.items(),
                key=lambda entry: len(entry[1]),
                reverse=True,
            ):
                if len(album_tracks) < 4:
                    continue
                album_key = normalize_album_title(album_title).replace(" ", "-")
                generated_playlists.append(
                    {
                        "id": (
                            f"search-generated:{stable_artist_key}:"
                            f"album:{album_key}"
                        ),
                        "name": f"{album_title} by {artist_name}",
                        "title": f"{album_title} by {artist_name}",
                        "subtitle": f"Tracks from {album_title}.",
                        "provider": "neatie",
                        "generated": True,
                        "track_count": len(album_tracks[:24]),
                        "tracks": album_tracks[:24],
                        "thumbnail": (
                            album_tracks[0].get("thumbnail")
                            or lead_artist.get("thumbnail")
                        ),
                    }
                )
                if len(generated_playlists) >= 6:
                    break
            relevant_playlists = self._merge_snapshot_items(
                "playlists",
                relevant_playlists,
                generated_playlists,
            )
        catalog_status = str(
            catalog.get("catalog_status") or "retryable"
        ).strip().lower()
        if catalog_status != "complete":
            catalog_status = "retryable"
        return {
            "lead_artist": lead_artist,
            "tracks": canonical_tracks,
            "albums": canonical_albums,
            "artists": [lead_artist],
            "artist_tracks": canonical_tracks,
            "artist_albums": canonical_albums,
            "related_artists": resolved_related,
            "related_albums": _artist_catalog_albums(
                resolved_related,
                relationship="related_artist_discography",
            ),
            "playlists": relevant_playlists,
            "catalog_status": catalog_status,
            "related_status": related_status,
            "playlists_complete": provider_playlists_complete,
            "lastfm_ms": lastfm_ms,
            "related_artwork_ms": related_artwork_ms,
            "catalog_source": catalog_source,
            "local_catalog_ms": local_catalog_ms,
            "live_catalog_ms": live_catalog_ms,
            "album_tracklists_loaded": int(
                catalog.get("album_tracklists_loaded") or 0
            ),
        }

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

    @staticmethod
    def _merge_snapshot_items(
        surface: str,
        current: List[Dict[str, Any]],
        incoming: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        def identity(item: Dict[str, Any]) -> str:
            if surface == "related_artists":
                musicbrainz_id = _artist_musicbrainz_id(item)
                if musicbrainz_id:
                    return f"mbid:{musicbrainz_id}"
                name_key = _artist_name_key(item)
                return f"name:{name_key}" if name_key else _artist_merge_key(item)
            if surface == "artists":
                return _artist_merge_key(item)
            if surface == "albums":
                return str(
                    item.get("canonical_album_identity")
                    or item.get("id")
                    or (
                        f"{item.get('title') or ''}|"
                        f"{item.get('artist') or item.get('artist_name') or ''}"
                    )
                ).casefold()
            if surface == "playlists":
                return str(item.get("id") or item.get("name") or "").casefold()
            return str(
                item.get("canonical_track_identity")
                or item.get("track_key")
                or item.get("id")
                or ""
            )

        output: List[Dict[str, Any]] = []
        positions: Dict[str, int] = {}
        for raw in [*current, *incoming]:
            item = dict(raw or {})
            key = identity(item)
            if not key:
                continue
            existing_index = positions.get(key)
            if (
                existing_index is None
                and surface in {"artists", "related_artists"}
            ):
                existing_index = next(
                    (
                        index
                        for index, existing in enumerate(output)
                        if _same_artist_identity(existing, item)
                    ),
                    None,
                )
            if existing_index is None:
                positions[key] = len(output)
                output.append(item)
                continue
            existing = output[existing_index]
            # Enrichment may add canonical ids, artwork and statistics. Keep
            # the original rank but merge the richer record into that slot.
            output[existing_index] = {
                **existing,
                **{
                    field: value
                    for field, value in item.items()
                    if value not in (None, "", [], {})
                },
            }
            positions[key] = existing_index
        return output

    def _expand_search_snapshot_surface(
        self,
        *,
        req,
        query: str,
        search_mode: str,
        surface: str,
        snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        lead_artist = dict(snapshot.get("lead_artist") or {})
        query_intent = str(
            snapshot.get("query_intent") or ""
        ).strip().lower()
        if lead_artist:
            previous_tracks = list(snapshot.get("tracks") or [])
            previous_albums = list(snapshot.get("albums") or [])
            completed = self._complete_artist_search_surfaces(
                query=query,
                user_scope_id=req.user_scope_id or "guest",
                lead_artist=lead_artist,
                tracks=list(snapshot.get("tracks") or []),
                albums=list(snapshot.get("albums") or []),
                related_artists=list(
                    snapshot.get("related_artists") or []
                ),
                playlists=list(snapshot.get("playlists") or []),
                limit=120,
            )
            for key in (
                "lead_artist",
                "artists",
                "artist_tracks",
                "artist_albums",
                "related_artists",
                "related_albums",
                "playlists",
            ):
                snapshot[key] = completed[key]
            if query_intent == "artist":
                snapshot["tracks"] = completed["tracks"]
                snapshot["albums"] = completed["albums"]
            else:
                snapshot["tracks"] = self._merge_snapshot_items(
                    "tracks",
                    previous_tracks,
                    completed["artist_tracks"],
                )
                snapshot["albums"] = self._merge_snapshot_items(
                    "albums",
                    [
                        album
                        for album in previous_albums
                        if _artist_item_matches(
                            album,
                            completed["lead_artist"],
                        )
                    ],
                    completed["artist_albums"],
                )
            catalog_complete = completed["catalog_status"] == "complete"
            related_complete = completed["related_status"] == "complete"
            expansion_state = dict(snapshot.get("expansion_state") or {})
            expansion_state.update(
                {
                    "tracks": (
                        "complete" if catalog_complete else "retryable"
                    ),
                    "albums": (
                        "complete" if catalog_complete else "retryable"
                    ),
                    "artists": (
                        "complete"
                        if catalog_complete and related_complete
                        else "retryable"
                    ),
                    "playlists": (
                        "complete"
                        if completed["playlists"]
                        else "retryable"
                    ),
                }
            )
            snapshot["expansion_state"] = expansion_state
            print(
                "[EBB:search][expand] "
                f"query={query[:48]} surface={surface} "
                f"catalog_status={completed['catalog_status']} "
                f"tracks={len(completed['artist_tracks'])} "
                f"albums={len(completed['artist_albums'])} "
                f"related_artists={len(completed['related_artists'])} "
                f"playlists={len(completed['playlists'])}",
                flush=True,
            )
            return snapshot
        if surface == "playlists":
            snapshot["playlists"] = self._merge_snapshot_items(
                "playlists",
                list(snapshot.get("playlists") or []),
                search_playlists_direct(query, 72, server=self._server),
            )
            snapshot["playlists_loaded"] = True
        else:
            canonical_req = SimpleNamespace(
                query=query,
                surface="search",
                force_refresh=False,
                search_mode=search_mode,
                anchor_track_snapshots=[],
                recent_track_snapshots=[],
                last_played_tracks=[],
                recent_queries=[],
                taste_queries=[],
                artist_hints=[],
                result_type=surface,
                defer_side_surfaces=False,
            )
            retrieval_payload = retrieve_search_candidates_fast(
                canonical_req,
                {
                    "user_scope_id": req.user_scope_id or "guest",
                    "recent_queries": [],
                    "last_played_tracks": [],
                },
                limit=120 if surface == "tracks" else 96,
                server=self._server,
            )
            if surface == "tracks":
                expanded = rank_track_candidates_fast_path(
                    self._server,
                    canonical_req,
                    retrieval_payload,
                    limit=120,
                )
                snapshot["tracks"] = self._merge_snapshot_items(
                    "tracks",
                    list(snapshot.get("tracks") or []),
                    expanded,
                )
            elif surface == "artists":
                expanded = self._hydrate_artist_artwork(
                    rank_artist_candidates_fast_path(
                        self._server,
                        canonical_req,
                        retrieval_payload,
                        limit=72,
                    ),
                    allow_live_lead_lookup=False,
                )
                related = self._hydrate_artist_artwork(
                    list(retrieval_payload.get("related_artists") or []),
                    allow_live_lead_lookup=False,
                )
                lead_for_relationships = dict(
                    snapshot.get("lead_artist") or {}
                )
                try:
                    lastfm_related = self._lastfm_related_artists(
                        lead_for_relationships,
                        limit=16,
                    )
                    print(
                        "[EBB:search][expand] "
                        f"query={query[:48]} surface=artists "
                        f"lastfm_status=ok results={len(lastfm_related)}",
                        flush=True,
                    )
                except Exception as exc:
                    lastfm_related = []
                    print(
                        "[EBB:search][expand] "
                        f"query={query[:48]} surface=artists "
                        f"lastfm_status=failed error={str(exc)[:96]}",
                        flush=True,
                    )
                if lastfm_related:
                    related = self._hydrate_artist_artwork(
                        [*lastfm_related, *related],
                        allow_live_lead_lookup=False,
                    )
                snapshot["artists"] = self._merge_snapshot_items(
                    "artists",
                    list(snapshot.get("artists") or []),
                    expanded,
                )
                lead_artist = dict(snapshot.get("lead_artist") or {})
                if lead_artist:
                    richer_lead = next(
                        (
                            artist
                            for artist in snapshot["artists"]
                            if _same_artist_identity(artist, lead_artist)
                        ),
                        None,
                    )
                    if richer_lead is not None:
                        snapshot["lead_artist"] = {
                            **lead_artist,
                            **richer_lead,
                        }
                snapshot["related_artists"] = self._merge_snapshot_items(
                    "related_artists",
                    list(snapshot.get("related_artists") or []),
                    related,
                )
                selected_lead = dict(snapshot.get("lead_artist") or {})
                ranked_tracks = rank_track_candidates_fast_path(
                    self._server,
                    canonical_req,
                    retrieval_payload,
                    limit=72,
                )
                ranked_albums = rank_album_candidates_fast_path(
                    self._server,
                    canonical_req,
                    retrieval_payload,
                    limit=72,
                )
                artist_tracks = [
                    item
                    for item in ranked_tracks
                    if not selected_lead
                    or _artist_item_matches(item, selected_lead)
                ]
                artist_albums = [
                    item
                    for item in ranked_albums
                    if (
                        bool(
                            set(item.get("search_sources") or [])
                            & {
                                "artist_discography",
                                "credited_artist_discography",
                            }
                        )
                        and (
                            not selected_lead
                            or _artist_item_matches(item, selected_lead)
                        )
                    )
                ]
                snapshot["artist_tracks"] = self._merge_snapshot_items(
                    "tracks",
                    list(snapshot.get("artist_tracks") or []),
                    artist_tracks,
                )
                snapshot["artist_albums"] = self._merge_snapshot_items(
                    "albums",
                    list(snapshot.get("artist_albums") or []),
                    artist_albums,
                )
                snapshot["related_albums"] = self._merge_snapshot_items(
                    "albums",
                    list(snapshot.get("related_albums") or []),
                    _artist_catalog_albums(
                        list(snapshot.get("related_artists") or []),
                        relationship="related_artist_discography",
                    ),
                )
            elif surface == "albums":
                expanded = rank_album_candidates_fast_path(
                    self._server,
                    canonical_req,
                    retrieval_payload,
                    limit=72,
                )
                snapshot["albums"] = self._merge_snapshot_items(
                    "albums",
                    list(snapshot.get("albums") or []),
                    expanded,
                )
        expansion_state = dict(snapshot.get("expansion_state") or {})
        if surface == "artists" and self._missing_artist_artwork(snapshot):
            expansion_state[surface] = "pending_artwork"
            artwork_poll_attempts = dict(
                snapshot.get("artwork_poll_attempts") or {}
            )
            artwork_poll_attempts[surface] = 0
            snapshot["artwork_poll_attempts"] = artwork_poll_attempts
        else:
            expansion_state[surface] = "complete"
        snapshot["expansion_state"] = expansion_state
        print(
            "[EBB:search][expand] "
            f"query={query[:48]} surface={surface} "
            f"artists={len(snapshot.get('artists') or [])} "
            f"related_artists={len(snapshot.get('related_artists') or [])} "
            f"artist_tracks={len(snapshot.get('artist_tracks') or [])} "
            f"artist_albums={len(snapshot.get('artist_albums') or [])} "
            f"related_albums={len(snapshot.get('related_albums') or [])}",
            flush=True,
        )
        return snapshot

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
        artist_albums: List[Dict[str, Any]] | None = None,
        related_albums: List[Dict[str, Any]] | None = None,
        lead_artist: Dict[str, Any] | None = None,
        containing_album: Dict[str, Any] | None = None,
        playlists: List[Dict[str, Any]] | None = None,
        pagination: Dict[str, Any] | None = None,
        enrichment_applied: bool = False,
        enrichment_quality_score: float = 0.0,
        enrichment_elapsed_ms: int = 0,
        enrichment_completed_surfaces: List[str] | None = None,
        enrichment_timed_out_surfaces: List[str] | None = None,
        write_resolution_memory: bool = True,
    ) -> Dict[str, Any]:
        search = self._search_server()
        similar_tracks = list(similar_tracks or [])
        artist_tracks = list(artist_tracks or [])
        artist_albums = list(artist_albums or [])
        related_albums = list(related_albums or [])
        playlists = list(playlists or [])
        enrichment_completed_surfaces = list(enrichment_completed_surfaces or [])
        enrichment_timed_out_surfaces = list(enrichment_timed_out_surfaces or [])
        top_result = None
        preferred = {
            "track": tracks,
            "artist": artists,
            "album": albums,
        }.get(query_intent) or []
        if preferred:
            top_result = {"entity_type": query_intent, "item": preferred[0]}
        elif tracks:
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
        search.trace_put(trace, "candidate_counts", "search.artist_albums", len(artist_albums))
        search.trace_put(trace, "candidate_counts", "search.related_albums", len(related_albums))
        response = {
            "status": "success",
            "request_id": trace["request_id"],
            "model_version": track_model_version,
            "query_intent": query_intent,
            "top_result": top_result,
            "lead_artist": lead_artist,
            "containing_album": containing_album,
            "results": tracks[:limit],
            "tracks": tracks[:limit],
            "artists": artists[:limit],
            "albums": albums[:limit],
            "playlists": playlists[:limit],
            "similar_artists": similar_artists[:limit],
            "similar_tracks": similar_tracks[: max(1, min(8, limit))],
            "artist_tracks": artist_tracks[: max(24, min(48, limit))],
            "artist_albums": artist_albums[: max(1, min(16, limit))],
            "related_albums": related_albums[: max(1, min(8, limit))],
            "pagination": dict(pagination or {}),
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
            top_item = dict(top_result.get("item") or {})
            entity_type = str(top_result.get("entity_type") or "track")
            candidate_text = (
                top_item.get("name")
                if entity_type == "artist"
                else top_item.get("title") or top_item.get("name")
            )
            automatic_confidence = search_text_similarity(
                req.query or "",
                str(candidate_text or ""),
            )
            response["resolved_entity_type"] = entity_type
            response["resolved_entity_key"] = catalog_entity_key(
                entity_type,
                top_item,
                query=req.query or "",
            )
            response["resolved_entity_confidence"] = round(
                automatic_confidence,
                4,
            )
            if automatic_confidence >= 0.78 and write_resolution_memory:
                response["diagnostics"]["query_memory_written"] = (
                    remember_search_resolution(
                        self._server,
                        user_scope_id=req.user_scope_id or "guest",
                        query=req.query or "",
                        entity_type=entity_type,
                        item=top_item,
                        confidence=automatic_confidence,
                        event_weight=0.1,
                        event_type="search_resolution",
                        source="canonical_search_response",
                    )
                )
        search.trace_log_request(
            trace,
            request_type="search",
            user_scope_id=req.user_scope_id or "guest",
            model_version=track_model_version,
        )
        return response

    def _search_canonical_entities(
        self,
        *,
        req,
        trace: Dict[str, Any],
        query: str,
        limit: int,
        search_mode: str,
        track_model_version: str,
    ) -> Dict[str, Any]:
        server = self._server
        direct_started_at = time.perf_counter()
        requested_surface = str(
            getattr(req, "result_type", "") or ""
        ).strip().lower()
        request_offset = max(int(getattr(req, "offset", 0) or 0), 0)
        snapshot_key = _search_snapshot_key(
            req.user_scope_id or "guest",
            query,
            search_mode,
        )
        snapshot = (
            None
            if bool(getattr(req, "force_refresh", False))
            else _load_search_snapshot(snapshot_key)
        )
        if snapshot is not None:
            current_expansion_state = dict(
                snapshot.get("expansion_state") or {}
            )
            artwork_refresh = (
                requested_surface == "artists"
                and current_expansion_state.get(requested_surface)
                == "pending_artwork"
            )
            refreshed_snapshot = self._rehydrate_search_snapshot(
                snapshot,
                schedule_background=artwork_refresh,
            )
            if refreshed_snapshot != snapshot:
                snapshot = refreshed_snapshot
                _store_search_snapshot(snapshot_key, snapshot)
            expansion_state = dict(snapshot.get("expansion_state") or {})
            if artwork_refresh:
                artwork_poll_attempts = dict(
                    snapshot.get("artwork_poll_attempts") or {}
                )
                attempt = int(
                    artwork_poll_attempts.get(requested_surface) or 0
                ) + 1
                artwork_poll_attempts[requested_surface] = attempt
                snapshot["artwork_poll_attempts"] = artwork_poll_attempts
                if not self._missing_artist_artwork(snapshot):
                    expansion_state[requested_surface] = "complete"
                    snapshot["expansion_state"] = expansion_state
                _store_search_snapshot(snapshot_key, snapshot)
            surface_items = list(snapshot.get(requested_surface) or [])
            page_size = max(8, min(int(limit or 16), 24))
            should_expand = (
                expansion_state.get(requested_surface) in {"pending", "retryable"}
                and request_offset >= len(surface_items)
            )
            if should_expand:
                try:
                    snapshot = self._expand_search_snapshot_surface(
                        req=req,
                        query=query,
                        search_mode=search_mode,
                        surface=requested_surface,
                        snapshot=snapshot,
                    )
                except Exception:
                    expansion_state = dict(
                        snapshot.get("expansion_state") or {}
                    )
                    expansion_state[requested_surface] = "retryable"
                    snapshot["expansion_state"] = expansion_state
                _store_search_snapshot(snapshot_key, snapshot)
            pages: Dict[str, Any] = {}
            paged: Dict[str, List[Dict[str, Any]]] = {}
            for surface_name in (
                "tracks",
                "artists",
                "albums",
                "playlists",
                "related_artists",
            ):
                surface_items = list(snapshot.get(surface_name) or [])
                surface_offset = (
                    0
                    if (
                        (should_expand or artwork_refresh)
                        and requested_surface == surface_name
                    )
                    else request_offset
                    if requested_surface == surface_name
                    else 0
                )
                paged[surface_name], pages[surface_name] = self._surface_page(
                    surface_items,
                    offset=surface_offset,
                    limit=page_size,
                )
                state_value = dict(
                    snapshot.get("expansion_state") or {}
                ).get(surface_name)
                if (
                    surface_name == requested_surface
                    and state_value in {
                        "pending",
                        "retryable",
                        "pending_artwork",
                    }
                ):
                    pages[surface_name]["has_more"] = True
                    pages[surface_name]["deferred_expansion"] = True
            response = self._build_direct_search_response(
                req=req,
                trace=trace,
                query_intent=str(snapshot.get("query_intent") or "mixed"),
                limit=limit,
                track_model_version=track_model_version,
                tracks=paged["tracks"],
                artists=paged["artists"],
                albums=paged["albums"],
                similar_artists=paged["related_artists"],
                artist_tracks=deepcopy(snapshot.get("artist_tracks") or []),
                artist_albums=deepcopy(snapshot.get("artist_albums") or []),
                related_albums=deepcopy(snapshot.get("related_albums") or []),
                lead_artist=deepcopy(snapshot.get("lead_artist")),
                containing_album=deepcopy(snapshot.get("containing_album")),
                playlists=paged["playlists"],
                pagination=pages,
                direct_lookup_ms=int(
                    (time.perf_counter() - direct_started_at) * 1000
                ),
                write_resolution_memory=False,
            )
            diagnostics = dict(response.get("diagnostics") or {})
            diagnostics.update(
                {
                    "ranking_backend": "canonical_search_snapshot_v1",
                    "query_mode": search_mode,
                    "search_snapshot_hit": True,
                    "profile_build_skipped": True,
                    "relevance_admission": True,
                }
            )
            response["diagnostics"] = diagnostics
            return response

        canonical_req = SimpleNamespace(
            query=query,
            surface="search",
            force_refresh=bool(getattr(req, "force_refresh", False)),
            search_mode=search_mode,
            anchor_track_snapshots=[],
            recent_track_snapshots=[],
            last_played_tracks=[],
            recent_queries=[],
            taste_queries=[],
            artist_hints=[],
            result_type=str(getattr(req, "result_type", "") or ""),
            defer_side_surfaces=bool(
                getattr(req, "defer_side_surfaces", False)
            ),
        )
        candidate_limit = (
            min(max(request_offset + (limit * 3), 48), 120)
            if requested_surface
            else 48
        )
        retrieval_payload = retrieve_search_candidates_fast(
            canonical_req,
            {
                "user_scope_id": req.user_scope_id or "guest",
                "recent_queries": [],
                "last_played_tracks": [],
            },
            limit=candidate_limit,
            server=server,
        )
        query_intent = str(retrieval_payload.get("query_intent") or "mixed")
        tracks = rank_track_candidates_fast_path(
            server,
            canonical_req,
            retrieval_payload,
            limit=candidate_limit,
        )
        artists = rank_artist_candidates_fast_path(
            server,
            canonical_req,
            retrieval_payload,
            limit=36,
        )
        albums = rank_album_candidates_fast_path(
            server,
            canonical_req,
            retrieval_payload,
            limit=36,
        )
        artists = self._hydrate_artist_artwork(
            artists,
            allow_live_lead_lookup=False,
            schedule_background=False,
        )
        artists = self._merge_snapshot_items("artists", [], artists)
        related_artists = self._hydrate_artist_artwork(
            list(retrieval_payload.get("related_artists") or []),
            allow_live_lead_lookup=False,
        )
        related_artists = self._merge_snapshot_items(
            "related_artists",
            [],
            related_artists,
        )
        playlists = list(retrieval_payload.get("playlists") or [])
        if requested_surface == "playlists":
            playlists = search_playlists_direct(query, 36, server=server)
        lead_artist, containing_album = self._lead_entities(
            query=query,
            tracks=tracks,
            artists=artists,
            albums=albums,
            query_intent=query_intent,
            resolved_artist=dict(
                retrieval_payload.get("resolved_artist") or {}
            ),
        )
        if lead_artist:
            hydrated_lead = self._hydrate_artist_artwork(
                [lead_artist],
                allow_live_lead_lookup=False,
                schedule_background=False,
            )
            lead_artist = hydrated_lead[0] if hydrated_lead else lead_artist
        if isinstance(lead_artist, dict):
            if query_intent == "artist":
                query_alias = normalize_artist_name(query)
                aliases = list(lead_artist.get("artist_aliases") or [])
                if query_alias and query_alias not in aliases:
                    aliases.append(query_alias)
                lead_artist["artist_aliases"] = aliases
            lead_position = next(
                (
                    index
                    for index, artist in enumerate(artists)
                    if _same_artist_identity(artist, lead_artist)
                    or (
                        _raw_artist_name(artist.get("name"))
                        == _raw_artist_name(lead_artist.get("name"))
                    )
                ),
                None,
            )
            if lead_position is not None:
                lead_artist = _merge_artist_values(
                    artists[lead_position],
                    lead_artist,
                )
                artists[lead_position] = lead_artist
                if lead_position:
                    artists.insert(0, artists.pop(lead_position))
            else:
                artists.insert(0, lead_artist)
        lastfm_ms = 0
        related_artwork_ms = 0
        catalog_status = "complete"
        related_status = "complete"
        playlists_complete = bool(playlists)
        album_tracklists_loaded = 0
        catalog_source = "none"
        local_catalog_ms = 0
        live_catalog_ms = 0
        defer_side_surfaces = bool(
            getattr(req, "defer_side_surfaces", False)
        )
        if isinstance(lead_artist, dict) and not defer_side_surfaces:
            completed_artist = self._complete_artist_search_surfaces(
                query=query,
                user_scope_id=req.user_scope_id or "guest",
                lead_artist=lead_artist,
                tracks=tracks,
                albums=albums,
                related_artists=related_artists,
                playlists=playlists,
                limit=candidate_limit,
            )
            lead_artist = dict(completed_artist["lead_artist"])
            artists = list(completed_artist["artists"])
            artist_tracks = list(completed_artist["artist_tracks"])
            artist_albums = list(completed_artist["artist_albums"])
            related_artists = list(completed_artist["related_artists"])
            related_albums = list(completed_artist["related_albums"])
            playlists = list(completed_artist["playlists"])
            catalog_status = str(completed_artist["catalog_status"])
            related_status = str(completed_artist["related_status"])
            playlists_complete = bool(completed_artist["playlists_complete"])
            lastfm_ms = int(completed_artist["lastfm_ms"])
            related_artwork_ms = int(
                completed_artist["related_artwork_ms"]
            )
            album_tracklists_loaded = int(
                completed_artist["album_tracklists_loaded"]
            )
            catalog_source = str(completed_artist["catalog_source"])
            local_catalog_ms = int(completed_artist["local_catalog_ms"])
            live_catalog_ms = int(completed_artist["live_catalog_ms"])
            if query_intent == "artist":
                tracks = list(completed_artist["tracks"])
                albums = list(completed_artist["albums"])
            else:
                # A track or album query still owns one canonical lead artist.
                # Preserve the exact query matches first, then continue with
                # that artist's real catalog instead of the generic provider
                # batch that happened to contain the query text.
                tracks = self._merge_snapshot_items(
                    "tracks",
                    tracks,
                    artist_tracks,
                )
                primary_albums = [
                    album
                    for album in [
                        containing_album,
                        *albums,
                    ]
                    if isinstance(album, dict)
                    and _artist_item_matches(album, lead_artist)
                ]
                albums = self._merge_snapshot_items(
                    "albums",
                    primary_albums,
                    artist_albums,
                )
        elif isinstance(lead_artist, dict):
            artist_tracks = [
                item
                for item in tracks
                if _artist_item_matches(item, lead_artist)
            ]
            artist_albums = [
                item
                for item in albums
                if _artist_item_matches(item, lead_artist)
            ]
            related_albums = _artist_catalog_albums(
                related_artists,
                relationship="related_artist_discography",
            )
            catalog_status = "retryable"
            related_status = "retryable"
            playlists_complete = False
        else:
            artist_tracks = []
            artist_albums = []
            related_albums = []
            catalog_status = "retryable"
            related_status = "retryable"
            playlists_complete = False

        tracks, albums = _repair_search_artwork(tracks, albums)
        artist_tracks, artist_albums = _repair_search_artwork(
            artist_tracks,
            artist_albums,
        )
        albums = [
            album for album in albums if catalog_album_is_detail_ready(album)
        ]
        artist_albums = [
            album
            for album in artist_albums
            if catalog_album_is_detail_ready(album)
        ]

        snapshot = {
            "query_intent": query_intent,
            "tracks": tracks,
            "artists": artists,
            "albums": albums,
            "related_artists": related_artists,
            "artist_tracks": artist_tracks,
            "artist_albums": artist_albums,
            "related_albums": related_albums,
            "playlists": playlists,
            "playlists_loaded": bool(playlists),
            "lead_artist": lead_artist,
            "containing_album": containing_album,
            "expansion_state": {
                "tracks": (
                    "retryable"
                    if isinstance(lead_artist, dict)
                    and catalog_status != "complete"
                    else "pending"
                    if bool(getattr(req, "defer_side_surfaces", False))
                    else "complete"
                ),
                "artists": (
                    "retryable"
                    if isinstance(lead_artist, dict)
                    and (
                        catalog_status != "complete"
                        or related_status != "complete"
                    )
                    else "pending"
                    if bool(getattr(req, "defer_side_surfaces", False))
                    else "complete"
                ),
                "albums": (
                    "retryable"
                    if isinstance(lead_artist, dict)
                    and catalog_status != "complete"
                    else "pending"
                    if bool(getattr(req, "defer_side_surfaces", False))
                    else "complete"
                ),
                "playlists": (
                    "retryable"
                    if isinstance(lead_artist, dict)
                    and not playlists_complete
                    else "pending"
                    if bool(getattr(req, "defer_side_surfaces", False))
                    else "complete"
                ),
            },
        }
        _store_search_snapshot(snapshot_key, snapshot)
        offset = request_offset
        page_size = max(8, min(int(limit or 16), 24))
        pages: Dict[str, Any] = {}
        paged_tracks, pages["tracks"] = self._surface_page(
            tracks, offset=offset if requested_surface == "tracks" else 0,
            limit=page_size,
        )
        paged_artists, pages["artists"] = self._surface_page(
            artists, offset=offset if requested_surface == "artists" else 0,
            limit=page_size,
        )
        paged_albums, pages["albums"] = self._surface_page(
            albums, offset=offset if requested_surface == "albums" else 0,
            limit=page_size,
        )
        paged_playlists, pages["playlists"] = self._surface_page(
            playlists, offset=offset if requested_surface == "playlists" else 0,
            limit=page_size,
        )
        paged_related, pages["related_artists"] = self._surface_page(
            related_artists,
            offset=0,
            limit=page_size,
        )
        if not requested_surface:
            for surface_name, state_value in dict(
                snapshot.get("expansion_state") or {}
            ).items():
                if state_value not in {"pending", "retryable"}:
                    continue
                if surface_name in pages:
                    pages[surface_name]["has_more"] = True
                    pages[surface_name]["deferred_expansion"] = True
        direct_lookup_ms = int((time.perf_counter() - direct_started_at) * 1000)
        print(
            "[EBB:search][progress] "
            f"request_id={trace.get('request_id') or ''} "
            f"stage=canonical_search done intent={query_intent} "
            f"lead={str((lead_artist or {}).get('name') or '')[:32]} "
            f"tracks={len(tracks)} artists={len(artists)} "
            f"albums={len(albums)} artist_tracks={len(artist_tracks)} "
            f"artist_albums={len(artist_albums)} related_artists={len(related_artists)} "
            f"playlists={len(playlists)} catalog_status={catalog_status} "
            f"catalog_source={catalog_source} local_ms={local_catalog_ms} "
            f"live_ms={live_catalog_ms} album_tracklists={album_tracklists_loaded} "
            f"lookup_ms={direct_lookup_ms}",
            flush=True,
        )
        _SEARCH_CATALOG_WRITER.submit(
            _cache_search_payload_background,
            server=self._server,
            query=query,
            tracks=tracks,
            artists=artists,
            albums=albums,
        )
        response = self._build_direct_search_response(
            req=req,
            trace=trace,
            query_intent=query_intent,
            limit=limit,
            track_model_version=track_model_version,
            tracks=paged_tracks,
            artists=paged_artists,
            albums=paged_albums,
            similar_artists=paged_related,
            artist_tracks=artist_tracks,
            artist_albums=artist_albums,
            related_albums=related_albums,
            lead_artist=lead_artist,
            containing_album=containing_album,
            playlists=paged_playlists,
            pagination=pages,
            direct_lookup_ms=direct_lookup_ms,
        )
        diagnostics = dict(response.get("diagnostics") or {})
        diagnostics["ranking_backend"] = "canonical_search_v1"
        diagnostics["query_mode"] = search_mode
        diagnostics["retrieval"] = dict(
            retrieval_payload.get("retrieval_diagnostics") or {}
        )
        diagnostics["surface_timings_ms"] = {
            "local_catalog": local_catalog_ms,
            "live_catalog": live_catalog_ms,
            "lastfm_related": lastfm_ms,
            "related_artist_artwork": related_artwork_ms,
        }
        diagnostics["artist_catalog"] = {
            "status": catalog_status,
            "source": catalog_source,
            "track_count": len(artist_tracks),
            "album_count": len(artist_albums),
            "playlist_count": len(playlists),
            "related_artist_count": len(related_artists),
            "album_tracklists_loaded": album_tracklists_loaded,
        }
        diagnostics["profile_build_skipped"] = True
        diagnostics["relevance_admission"] = True
        response["diagnostics"] = diagnostics
        return response

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
        limit = max(8, min(req.limit or 16, 24))
        track_model_version = "canonical_search_v1"
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
                        "artist_albums": [],
                        "related_albums": [],
                        "diagnostics": {
                            "ranking_backend": "canonical_search_v1",
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
                        "artist_albums": [],
                        "related_albums": [],
                        "diagnostics": {
                            "ranking_backend": "canonical_url_search_v1",
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
                return self._search_canonical_entities(
                    req=req,
                    trace=trace,
                    query=query,
                    limit=limit,
                    search_mode=search_mode,
                    track_model_version=track_model_version,
                )
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
            "model_version": "canonical_search_v1",
            "albums": list(response.get("albums") or [])[: max(1, min(req.limit or 12, 12))],
            "diagnostics": diagnostics,
        }

    def search_artists(self, req):
        response = self.search(req)
        diagnostics = dict(response.get("diagnostics") or {})
        return {
            "status": "success",
            "request_id": response.get("request_id") or "",
            "model_version": "canonical_search_v1",
            "artists": list(response.get("artists") or [])[: max(1, min(req.limit or 12, 12))],
            "diagnostics": diagnostics,
        }

    def playlist_details(self, playlist_id: str) -> Dict[str, Any]:
        normalized_id = str(playlist_id or "").strip()
        if not normalized_id:
            return {"status": "success", "playlist": {}, "tracks": []}
        payload = self._search_server().search_upstream_call_with_retry(
            lambda: self._server.ytmusic.get_playlist(
                playlistId=normalized_id,
                limit=100,
            ),
            default={},
        )
        payload = dict(payload or {})
        tracks: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw_track in payload.get("tracks") or []:
            track = self._search_server().normalize_track(raw_track)
            if track is None:
                continue
            track_id = str(track.get("id") or "").strip()
            if not track_id or track_id in seen:
                continue
            seen.add(track_id)
            tracks.append(track)
        playlist = {
            "id": normalized_id,
            "name": payload.get("title") or "Playlist",
            "title": payload.get("title") or "Playlist",
            "subtitle": payload.get("description") or payload.get("author") or "",
            "thumbnail": self._server.extract_thumbnail(payload),
            "author": self._server.extract_artist(payload),
            "track_count": len(tracks),
            "provider": "ytmusic",
            "tracks": tracks,
        }
        cache_search_payload(tracks=tracks, artists=[], albums=[])
        return {"status": "success", "playlist": playlist, "tracks": tracks}

    def resolve_artist(self, req):
        started_at = time.perf_counter()
        query = self._search_server().trim_text(req.query)
        requested_artist_id = self._search_server().trim_text(
            getattr(req, "anchor_artist_id", "")
        )
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
                if requested_artist_id
                and str(
                    artist.get("provider_artist_id")
                    or artist.get("id")
                    or ""
                ).strip()
                == requested_artist_id
            ),
            None,
        )
        if resolved is None:
            exact_matches = [
                artist
                for artist in artists
                if self._search_server().normalize_text(artist.get("name"))
                == normalized_query
            ]
            resolved = max(
                exact_matches,
                key=lambda artist: (
                    normalized_popularity(artist),
                    bool(str(artist.get("thumbnail") or "").strip()),
                ),
                default=artists[0] if artists else None,
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
        try:
            suggestion_items = semantic_search_suggestion_items(req, server=server)
            results = [
                item.get("text") if isinstance(item, dict) else str(item)
                for item in suggestion_items
                if (item.get("text") if isinstance(item, dict) else str(item))
            ]
            return {
                "status": "success",
                "results": results[: max(1, min(req.limit or 5, 8))],
                "suggestions": suggestion_items[: max(1, min(req.limit or 5, 8))],
                "diagnostics": {
                    "ranking_backend": "canonical_suggestions_v1",
                    "warmup_scheduled": False,
                },
            }
        except Exception:
            return {
                "status": "success",
                "results": [],
                "error_message": "Suggestions are temporarily unavailable.",
                "diagnostics": {"ranking_backend": "canonical_suggestions_v1"},
            }
