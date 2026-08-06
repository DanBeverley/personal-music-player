from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
    wait,
)
from copy import deepcopy
import re
import threading
import time
from types import SimpleNamespace
from typing import Any
from typing import Dict, List

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
    search_canonical_album_for_track,
    search_playlists_direct,
    search_query_intent,
    semantic_search_suggestion_items,
)
from .server_adapter import SearchServerAdapter
from .intelligence import (
    catalog_entity_key,
    load_catalog_artist_records,
    remember_catalog_entity,
    search_text_similarity,
)
from ..storage.artist_artwork import (
    attach_entity_artwork_proxy,
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
_SEARCH_RELATED_ARTIST_VISIBLE_TARGET = 6
_SEARCH_RELATED_ARTIST_RESOLUTION_BATCH = 3
_SEARCH_SNAPSHOT_COMPLETION_LOCK = threading.Lock()
_SEARCH_SNAPSHOT_COMPLETION_PENDING: set[str] = set()
_SEARCH_TARGET_ESSENTIAL_BUDGET_SECONDS = 1.4
_SEARCH_TARGET_REVALIDATION_MAX_ATTEMPTS = 2


def _search_snapshot_key(user_scope_id: str, query: str, search_mode: str) -> str:
    return "||".join(
        [
            str(user_scope_id or "guest").strip() or "guest",
            "canonical-target-search-v3",
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


def _search_snapshot_visible_fingerprint(snapshot: Dict[str, Any]) -> tuple:
    """Return customer-visible search content, excluding retry metadata."""

    def item_fingerprint(item: Any) -> tuple:
        if not isinstance(item, dict):
            return ()
        return tuple(
            str(item.get(key) or "").strip()
            for key in (
                "entity_type",
                "track_key",
                "canonical_recording_id",
                "canonical_artist_id",
                "musicbrainz_recording_id",
                "musicbrainz_artist_id",
                "musicbrainz_release_id",
                "musicbrainz_release_group_id",
                "provider_artist_id",
                "provider_album_id",
                "videoId",
                "browseId",
                "id",
                "title",
                "name",
                "artist",
                "channel",
                "thumbnail",
                "year",
                "release_year",
            )
        )

    resolved_target = dict(snapshot.get("resolved_target") or {})
    surfaces = (
        "tracks",
        "artists",
        "albums",
        "playlists",
        "related_artists",
        "artist_tracks",
        "artist_albums",
        "related_albums",
    )
    return (
        str(snapshot.get("query_intent") or ""),
        str(resolved_target.get("entity_type") or ""),
        str(resolved_target.get("target_identity") or ""),
        item_fingerprint(resolved_target.get("item")),
        item_fingerprint(snapshot.get("lead_artist")),
        item_fingerprint(snapshot.get("containing_album")),
        tuple(
            (
                surface,
                tuple(
                    item_fingerprint(item)
                    for item in list(snapshot.get(surface) or [])
                    if isinstance(item, dict)
                ),
            )
            for surface in surfaces
        ),
    )


def _target_quality(
    target: Dict[str, Any],
) -> tuple[float, float, float, float, float]:
    tier = str(target.get("confidence_tier") or "").strip().casefold()
    tier_score = {
        "authoritative": 4.0,
        "corroborated": 3.0,
        "supported": 2.0,
        "ambiguous": 0.0,
    }.get(tier, 1.0)
    canonical_score = 1.0 if (
        _target_identity(target).startswith("musicbrainz:")
        or "canonical_recording_credit" in set(target.get("evidence") or [])
    ) else 0.0
    return (
        tier_score,
        canonical_score,
        float(target.get("identity_confidence") or 0.0),
        float(target.get("confidence") or 0.0),
        float(target.get("decision_margin") or 0.0),
    )


def _target_identity(target: Dict[str, Any]) -> str:
    return str(target.get("target_identity") or "").strip().casefold()


def _target_has_recording_family_evidence(target: Dict[str, Any]) -> bool:
    if str(target.get("entity_type") or "").strip().casefold() != "track":
        return False
    evidence = {
        str(value or "").strip().casefold()
        for value in list(target.get("evidence") or [])
    }
    return bool(
        evidence.intersection(
            {
                "canonical_recording_credit",
                "provider_rank_dominance",
                "provider_structural_lead",
                "containing_album_relationship",
                "recording_family_comparison",
            }
        )
    )


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


def _catalog_alias_can_bridge_artist(
    artist: Dict[str, Any],
    persisted: Dict[str, Any],
) -> bool:
    if _same_artist_identity(artist, persisted):
        return True
    alias_confidence = float(
        persisted.get("_catalog_alias_confidence") or 0.0
    )
    alias_source = str(
        persisted.get("_catalog_alias_source") or ""
    ).strip()
    source_authority = str(
        persisted.get("source_authority") or ""
    ).strip()
    if not (
        _artist_provider_id(persisted)
        and alias_confidence >= 0.85
        and alias_source
        and source_authority
    ):
        return False
    artist_tokens = _artist_identity_tokens(artist)
    if not artist_tokens:
        return True
    if _artist_provider_id(artist) or _artist_musicbrainz_id(persisted):
        return False
    return bool(_artist_alias_keys(artist) & _artist_alias_keys(persisted))


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
    *,
    server: Any | None = None,
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
            if server is not None:
                album = attach_entity_artwork_proxy(
                    server,
                    album,
                    entity_type="album",
                )
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


def _search_album_is_publishable(album: Dict[str, Any]) -> bool:
    return (
        catalog_album_is_detail_ready(album)
        and bool(catalog_thumbnail_url(album, entity_type="album"))
    )


def _search_playlist_is_publishable(playlist: Dict[str, Any]) -> bool:
    return bool(catalog_thumbnail_url(playlist, entity_type="playlist"))


def _hydrate_containing_album_from_accepted_target(
    expected: Dict[str, Any],
    artist_albums: List[Dict[str, Any]],
    lead_artist: Dict[str, Any] | None,
    *,
    server: Any,
) -> Dict[str, Any]:
    """Resolve one accepted track's canonical release to a provider album."""
    expected = dict(expected or {})
    bound = _bind_containing_album_from_artist_catalog(
        expected,
        artist_albums,
        lead_artist,
    )
    if bound:
        return bound

    expected_title = str(
        expected.get("title")
        or expected.get("name")
        or expected.get("album")
        or ""
    ).strip()
    expected_artist = str(
        expected.get("artist")
        or expected.get("artist_name")
        or (lead_artist or {}).get("name")
        or ""
    ).strip()
    if not expected_title or not expected_artist:
        return {}

    resolved = search_canonical_album_for_track(
        {
            "album": expected_title,
            "channel": expected_artist,
            "artist": expected_artist,
            "thumbnail": expected.get("thumbnail"),
            "year": expected.get("year") or expected.get("release_year") or "",
        },
        server=server,
    )
    if not isinstance(resolved, dict) or not str(
        resolved.get("provider_album_id") or resolved.get("id") or ""
    ).strip():
        return {}
    resolved = {
        **expected,
        **{
            key: value
            for key, value in resolved.items()
            if value not in (None, "", [], {})
        },
    }
    return _bind_containing_album_from_artist_catalog(
        expected,
        [resolved],
        lead_artist,
    )


def _bind_containing_album_from_artist_catalog(
    expected: Dict[str, Any],
    artist_albums: List[Dict[str, Any]],
    lead_artist: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """Bind an accepted canonical release to one usable provider album."""
    expected = dict(expected or {})
    if not expected:
        return {}
    if catalog_album_is_detail_ready(expected):
        return expected
    expected_title = str(
        expected.get("title")
        or expected.get("name")
        or expected.get("album")
        or ""
    ).strip()
    expected_artist = str(
        expected.get("artist")
        or expected.get("artist_name")
        or (lead_artist or {}).get("name")
        or ""
    ).strip()
    expected_release_group = str(
        expected.get("musicbrainz_release_group_id") or ""
    ).strip().casefold()
    expected_release = str(
        expected.get("musicbrainz_release_id") or ""
    ).strip().casefold()

    def score(album: Dict[str, Any]) -> tuple[float, float, float]:
        if not catalog_album_is_detail_ready(album):
            return (0.0, 0.0, 0.0)
        release_group = str(
            album.get("musicbrainz_release_group_id") or ""
        ).strip().casefold()
        release_id = str(
            album.get("musicbrainz_release_id") or ""
        ).strip().casefold()
        canonical_match = bool(
            (expected_release_group and expected_release_group == release_group)
            or (expected_release and expected_release == release_id)
        )
        title_match = search_text_similarity(
            expected_title,
            str(album.get("title") or album.get("name") or "").strip(),
        )
        album_artist = str(
            album.get("artist") or album.get("artist_name") or ""
        ).strip()
        artist_match = (
            search_text_similarity(expected_artist, album_artist)
            if expected_artist and album_artist
            else 1.0
            if lead_artist and _artist_item_matches(album, lead_artist)
            else 0.0
        )
        accepted = canonical_match or (
            title_match >= 0.84 and artist_match >= 0.78
        )
        return (
            1.0 if accepted else 0.0,
            2.0 if canonical_match else title_match + artist_match,
            normalized_popularity(album),
        )

    candidates = [
        dict(album)
        for album in artist_albums
        if isinstance(album, dict)
    ]
    matched = max(candidates, key=score, default={})
    if not matched or score(matched)[0] <= 0.0:
        return {}
    merged = {**expected, **matched}
    for key in (
        "musicbrainz_release_id",
        "musicbrainz_release_group_id",
        "release_date",
        "release_year",
        "year",
    ):
        if expected.get(key) not in (None, "", [], {}):
            merged[key] = deepcopy(expected[key])
    merged["playable"] = True
    return merged


def _target_item_matches(
    entity_type: str,
    item: Dict[str, Any],
    expected: Dict[str, Any],
) -> bool:
    if not item or not expected:
        return False
    if entity_type == "artist":
        expected_id = _artist_provider_id(expected)
        return bool(expected_id and expected_id == _artist_provider_id(item))
    if entity_type == "album":
        expected_id = str(
            expected.get("provider_album_id")
            or expected.get("browseId")
            or expected.get("album_id")
            or expected.get("albumId")
            or expected.get("id")
            or ""
        ).strip().casefold()
        item_id = str(
            item.get("provider_album_id")
            or item.get("browseId")
            or item.get("album_id")
            or item.get("albumId")
            or item.get("id")
            or ""
        ).strip().casefold()
        return bool(expected_id and expected_id == item_id)
    if entity_type == "track":
        expected_source = verified_playback_source(expected)
        item_source = verified_playback_source(item)
        if expected_source and item_source:
            return expected_source == item_source
        expected_id = str(
            expected.get("videoId") or expected.get("id") or ""
        ).strip().casefold()
        item_id = str(item.get("videoId") or item.get("id") or "").strip().casefold()
        return bool(expected_id and expected_id == item_id)
    return False


def _materialize_resolved_target(
    resolved_target: Dict[str, Any],
    *,
    tracks: List[Dict[str, Any]],
    artists: List[Dict[str, Any]],
    albums: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Bind the resolver decision to ranked entities without guessing."""
    target = dict(resolved_target or {})
    entity_type = str(target.get("entity_type") or "mixed").strip().lower()
    expected = dict(target.get("item") or {})
    ranked = {
        "track": tracks,
        "artist": artists,
        "album": albums,
    }.get(entity_type, [])
    matched = next(
        (
            dict(item)
            for item in ranked
            if isinstance(item, dict)
            and _target_item_matches(entity_type, item, expected)
        ),
        {},
    )
    if (
        entity_type in {"track", "artist", "album"}
        and expected
        and not matched
        and target.get("ranked_target_validated") is True
        and str(target.get("target_identity") or "").strip()
    ):
        # Snapshot pagination may not include the first-page target in the
        # current slice. It was already bound before the snapshot was stored.
        return target
    if entity_type not in {"track", "artist", "album"} or not matched:
        return {
            **target,
            "entity_type": "mixed",
            "item": {},
            "lead_artist": {},
            "containing_album": {},
            "target_identity": "",
            "confidence_tier": "ambiguous",
            "evidence": [
                *list(target.get("evidence") or []),
                "ranked_target_missing",
            ],
        }

    materialized_item = {**expected, **matched}
    for key in (
        "track_key",
        "canonical_recording_id",
        "canonical_artist_id",
        "musicbrainz_recording_id",
        "musicbrainz_artist_id",
        "musicbrainz_artist_ids",
        "musicbrainz_release_id",
        "musicbrainz_release_group_id",
        "release_date",
        "release_year",
    ):
        if expected.get(key) not in (None, "", [], {}):
            materialized_item[key] = deepcopy(expected[key])
    target["item"] = materialized_item
    target["ranked_target_validated"] = True
    if entity_type == "artist":
        target["lead_artist"] = materialized_item
    else:
        expected_lead = dict(target.get("lead_artist") or {})
        expected_lead_id = _artist_provider_id(expected_lead)
        ranked_lead = next(
            (
                dict(artist)
                for artist in artists
                if expected_lead_id
                and _artist_provider_id(artist) == expected_lead_id
            ),
            {},
        )
        target["lead_artist"] = (
            _merge_artist_values(expected_lead, ranked_lead)
            if ranked_lead
            else expected_lead
        )
    if entity_type == "album":
        target["containing_album"] = materialized_item
    elif entity_type == "track":
        expected_album = dict(target.get("containing_album") or {})
        ranked_album = next(
            (
                dict(album)
                for album in albums
                if _target_item_matches("album", album, expected_album)
            ),
            {},
        )
        if ranked_album:
            materialized_album = {**expected_album, **ranked_album}
            for key in (
                "provider_album_id",
                "musicbrainz_release_id",
                "musicbrainz_release_group_id",
                "release_date",
                "release_year",
            ):
                if expected_album.get(key) not in (None, "", [], {}):
                    materialized_album[key] = deepcopy(expected_album[key])
            target["containing_album"] = materialized_album
        else:
            target["containing_album"] = expected_album
    return target


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
            visible_before = _search_snapshot_visible_fingerprint(snapshot)
            changed = False
            for surface in ("artists", "related_artists"):
                values = list(snapshot.get(surface) or [])
                for index, value in enumerate(values):
                    if (
                        not isinstance(value, dict)
                        or not _same_artist_identity(value, artist)
                    ):
                        continue
                    merged = {
                        **value,
                        **{
                            key: item
                            for key, item in artist.items()
                            if item not in (None, "", [], {})
                            and not str(key).startswith("_")
                        },
                    }
                    if merged != value:
                        values[index] = merged
                        changed = True
                snapshot[surface] = values
            lead_artist = snapshot.get("lead_artist")
            if (
                isinstance(lead_artist, dict)
                and _same_artist_identity(lead_artist, artist)
            ):
                merged_lead = {
                    **lead_artist,
                    **{
                        key: item
                        for key, item in artist.items()
                        if item not in (None, "", [], {})
                        and not str(key).startswith("_")
                    },
                }
                if merged_lead != lead_artist:
                    snapshot["lead_artist"] = merged_lead
                    changed = True
            resolved_target = dict(snapshot.get("resolved_target") or {})
            target_lead = dict(resolved_target.get("lead_artist") or {})
            artist_id = _artist_provider_id(artist)
            if artist_id and _artist_provider_id(target_lead) == artist_id:
                resolved_target["lead_artist"] = _merge_artist_values(
                    target_lead,
                    artist,
                )
                changed = True
            if (
                str(resolved_target.get("entity_type") or "").strip().lower()
                == "artist"
            ):
                target_item = dict(resolved_target.get("item") or {})
                if artist_id and _artist_provider_id(target_item) == artist_id:
                    merged_target = _merge_artist_values(target_item, artist)
                    resolved_target["item"] = merged_target
                    resolved_target["lead_artist"] = merged_target
                    changed = True
            if resolved_target:
                snapshot["resolved_target"] = resolved_target
            if changed and (
                _search_snapshot_visible_fingerprint(snapshot) != visible_before
            ):
                snapshot["revision"] = int(snapshot.get("revision") or 1) + 1


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


def _artist_metadata_pending_key(artist: Dict[str, Any]) -> str:
    return str(
        _artist_provider_id(artist)
        or _artist_merge_key(artist)
        or ""
    ).strip().casefold()


def _record_artist_resolution_attempt(artist: Dict[str, Any]) -> None:
    attempted = dict(artist or {})
    attempted["_provider_resolution_attempted_at"] = time.time()
    _update_search_snapshots_artist(attempted)


def _persist_search_artist(
    *,
    server: Any,
    query: str,
    artist: Dict[str, Any],
) -> None:
    artist = normalized_artist_payload(dict(artist or {}))
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
    _pending_reserved: bool = False,
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
    pending_key = _artist_metadata_pending_key(artist)
    if not pending_key:
        return None
    if not _pending_reserved:
        with _SEARCH_ARTIST_METADATA_LOCK:
            if pending_key in _SEARCH_ARTIST_METADATA_PENDING:
                return None
            _SEARCH_ARTIST_METADATA_PENDING.add(pending_key)
    try:
        cached_artist = attach_cached_artist_artwork(server, artist)
        cached_thumbnail = str(cached_artist.get("thumbnail") or "").strip()
        if cached_thumbnail.startswith("/artist_artwork/"):
            _persist_search_artist(
                server=server,
                query=query,
                artist=cached_artist,
            )
            if _artist_provider_id(cached_artist):
                return cached_artist
        if cached_thumbnail.startswith(("http://", "https://")):
            _persist_search_artist(
                server=server,
                query=query,
                artist=cached_artist,
            )
            schedule_artist_artwork_cache(
                server,
                cached_artist,
                on_cached=lambda cached: _persist_search_artist(
                    server=server,
                    query=query,
                    artist=cached,
                ),
            )
            if _artist_provider_id(cached_artist):
                return cached_artist
        artist = cached_artist
        artist_id = str(
            artist.get("provider_artist_id")
            or artist.get("browseId")
            or artist.get("artist_id")
            or artist.get("id")
            or ""
        ).strip()
        provider_id_is_usable = bool(_artist_provider_id(artist))
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
        if not _artist_provider_id(artist):
            _record_artist_resolution_attempt(artist)
            return None
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
            _record_artist_resolution_attempt(artist)
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


def _schedule_artist_metadata_resolution(
    *,
    server: Any,
    query: str,
    artist: Dict[str, Any],
) -> bool:
    pending_key = _artist_metadata_pending_key(artist)
    if not pending_key:
        return False
    with _SEARCH_ARTIST_METADATA_LOCK:
        if pending_key in _SEARCH_ARTIST_METADATA_PENDING:
            return False
        _SEARCH_ARTIST_METADATA_PENDING.add(pending_key)
    try:
        _SEARCH_ARTIST_METADATA_WRITER.submit(
            _resolve_artist_metadata_background,
            server=server,
            query=query,
            artist=artist,
            _pending_reserved=True,
        )
    except Exception:
        with _SEARCH_ARTIST_METADATA_LOCK:
            _SEARCH_ARTIST_METADATA_PENDING.discard(pending_key)
        return False
    return True


def _ensure_verified_artist_artwork(
    *,
    server: Any,
    query: str,
    artist: Dict[str, Any],
) -> Dict[str, Any]:
    """Reuse verified artwork or start the one job that can create it."""
    hydrated = attach_cached_artist_artwork(server, dict(artist or {}))
    thumbnail = str(hydrated.get("thumbnail") or "").strip()
    if thumbnail.startswith("/artist_artwork/"):
        return hydrated

    source_url = str(
        hydrated.get("artwork_source_url")
        or (thumbnail if thumbnail.startswith(("http://", "https://")) else "")
        or ""
    ).strip()
    if source_url and _artist_provider_id(hydrated):
        schedule_artist_artwork_cache(
            server,
            hydrated,
            on_cached=lambda cached: _persist_search_artist(
                server=server,
                query=query,
                artist=cached,
            ),
        )
    else:
        _schedule_artist_metadata_resolution(
            server=server,
            query=query,
            artist=hydrated,
        )
    return hydrated


def _cache_search_payload_background(
    *,
    server: Any,
    query: str,
    tracks: List[Dict[str, Any]],
    artists: List[Dict[str, Any]],
    albums: List[Dict[str, Any]],
    resolved_target: Dict[str, Any] | None = None,
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
    accepted_target = dict(resolved_target or {})
    accepted_type = str(accepted_target.get("entity_type") or "").strip().lower()
    accepted_item = dict(accepted_target.get("item") or {})
    accepted_album = dict(accepted_target.get("containing_album") or {})
    persisted_tracks = [
        *([accepted_item] if accepted_type == "track" and accepted_item else []),
        *tracks,
    ]
    persisted_albums = [
        *(
            [accepted_album]
            if accepted_type == "track" and accepted_album
            else [accepted_item]
            if accepted_type == "album" and accepted_item
            else []
        ),
        *albums,
    ]
    for entity_type, values, confidence, cap in (
        ("track", persisted_tracks, 0.84, 96),
        ("album", persisted_albums, 0.84, 48),
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
        schedule_artist_artwork_cache(
            server,
            artist,
            on_cached=lambda cached, search_query=query: _persist_search_artist(
                server=server,
                query=search_query,
                artist=cached,
            ),
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
        limit: int = _SEARCH_RELATED_ARTIST_VISIBLE_TARGET,
    ) -> List[Dict[str, Any]]:
        """Reuse canonical metadata and resolve only the next needed batch."""
        candidates = self._merge_snapshot_items(
            "related_artists",
            [],
            self._hydrate_artist_artwork(
                list(artists or []),
                allow_live_lead_lookup=False,
                schedule_background=False,
            ),
        )
        resolved_count = sum(
            1
            for artist in candidates
            if self._artist_has_usable_artwork(artist)
            and _artist_provider_id(artist)
        )
        target = max(int(limit or 0), 0)
        resolution_budget = min(
            max(target - resolved_count, 0),
            _SEARCH_RELATED_ARTIST_RESOLUTION_BATCH,
        )
        unresolved = sorted(
            (
                artist
                for artist in candidates
                if (
                    not self._artist_has_usable_artwork(artist)
                    or not _artist_provider_id(artist)
                )
            ),
            key=lambda artist: (
                float(
                    artist.get("_provider_resolution_attempted_at") or 0.0
                ),
                not bool(_artist_provider_id(artist)),
                not self._artist_has_usable_artwork(artist),
                -normalized_popularity(artist),
            ),
        )
        if candidates:
            _SEARCH_CATALOG_WRITER.submit(
                _cache_search_payload_background,
                server=self._server,
                query=query,
                tracks=[],
                artists=candidates,
                albums=[],
            )
        for artist in unresolved[:resolution_budget]:
            _schedule_artist_metadata_resolution(
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
        persisted_records = load_catalog_artist_records(
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
            identity_key = _artist_merge_key(artist)
            normalized_name = normalize_artist_name(
                artist.get("name")
                or artist.get("artist")
                or artist.get("channel")
            )
            persisted_identity_match = (
                persisted_records.get(identity_key) or {}
            )
            persisted_name_match = (
                persisted_records.get(normalized_name) or {}
            )
            authoritative_name_match = _catalog_alias_can_bridge_artist(
                artist,
                persisted_name_match,
            )
            if (
                persisted_identity_match
                and not _artist_provider_id(persisted_identity_match)
                and authoritative_name_match
            ):
                persisted = {
                    **persisted_identity_match,
                    **persisted_name_match,
                }
                for key in (
                    "musicbrainz_artist_id",
                    "artist_mbid",
                    "mb_artist_id",
                    "canonical_artist_id",
                    "canonical_artist_key",
                ):
                    canonical_value = (
                        persisted_identity_match.get(key) or artist.get(key)
                    )
                    if canonical_value:
                        persisted[key] = canonical_value
            else:
                persisted = (
                    persisted_identity_match
                    or persisted_by_id.get(artist_id)
                    or (
                        persisted_name_match
                        if authoritative_name_match
                        else None
                    )
                    or {}
                )
            provider_record = persisted_by_id.get(artist_id) or {}
            if provider_record:
                persisted = _merge_artist_values(persisted, provider_record)
            persisted_thumbnail = str(persisted.get("thumbnail") or "").strip()
            current_thumbnail = str(artist.get("thumbnail") or "").strip()
            if not current_thumbnail:
                artist["thumbnail"] = persisted_thumbnail or current_thumbnail
            current_provider_id = _artist_provider_id(artist)
            for key in (
                "canonical_artist_id",
                "musicbrainz_artist_id",
                "provider_artist_id",
                "artist_aliases",
                "artwork_source_url",
                "artwork_cache_token",
                "artwork_cache_identity",
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
            if persisted_id and bool(_artist_provider_id(persisted)):
                artist["provider_artist_id"] = persisted_id
            if persisted_id and not current_provider_id:
                artist["id"] = persisted_id
                artist_id = persisted_id
            artist = attach_cached_artist_artwork(self._server, artist)
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
                    artist = attach_cached_artist_artwork(
                        self._server,
                        artist,
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
                self._visible_artists(
                    list(refreshed.get("related_artists") or []),
                    dict(refreshed.get("lead_artist") or {}),
                ),
                relationship="related_artist_discography",
            ),
        )
        return refreshed

    def _artist_has_usable_artwork(self, artist: Dict[str, Any]) -> bool:
        resolved = attach_cached_artist_artwork(self._server, artist)
        thumbnail = str(resolved.get("thumbnail") or "").strip()
        return thumbnail.startswith("/artist_artwork/")

    def _visible_artists(
        self,
        artists: List[Dict[str, Any]],
        excluded_artist: Dict[str, Any] | None = None,
    ) -> List[Dict[str, Any]]:
        output: List[Dict[str, Any]] = []
        for artist in artists:
            if not isinstance(artist, dict):
                continue
            resolved = attach_cached_artist_artwork(self._server, artist)
            thumbnail = str(resolved.get("thumbnail") or "").strip()
            if not thumbnail.startswith("/artist_artwork/"):
                continue
            if not _artist_provider_id(resolved):
                continue
            if (
                isinstance(excluded_artist, dict)
                and _same_artist_identity(resolved, excluded_artist)
            ):
                continue
            output.append(resolved)
        return output

    def _missing_artist_artwork(self, snapshot: Dict[str, Any]) -> int:
        lead_artist = snapshot.get("lead_artist")
        artists = list(snapshot.get("artists") or [])[:8]
        if isinstance(lead_artist, dict):
            artists.append(lead_artist)
        unique_missing: set[str] = set()
        for artist in artists:
            if not isinstance(artist, dict):
                continue
            if self._artist_has_usable_artwork(artist):
                continue
            key = _artist_merge_key(artist)
            if key:
                unique_missing.add(key)
        related_candidates = [
            artist
            for artist in list(snapshot.get("related_artists") or [])
            if isinstance(artist, dict)
            and not (
                isinstance(lead_artist, dict)
                and _same_artist_identity(artist, lead_artist)
            )
        ]
        visible_related = self._visible_artists(
            related_candidates,
            lead_artist if isinstance(lead_artist, dict) else None,
        )
        related_target = min(6, len(related_candidates))
        return len(unique_missing) + max(
            related_target - len(visible_related),
            0,
        )

    def _snapshot_target_needs_revalidation(
        self,
        query: str,
        snapshot: Dict[str, Any],
    ) -> bool:
        state = str(
            snapshot.get("target_revalidation_state") or ""
        ).strip().casefold()
        attempts = int(snapshot.get("target_revalidation_attempts") or 0)
        target = dict(snapshot.get("resolved_target") or {})
        if not _target_identity(target):
            return True
        target_type = str(target.get("entity_type") or "").casefold()
        target_evidence = {
            str(value or "").strip().casefold()
            for value in list(target.get("evidence") or [])
        }
        if (
            target_type == "track"
            and "recording_family_comparison" not in target_evidence
            and attempts < _SEARCH_TARGET_REVALIDATION_MAX_ATTEMPTS
        ):
            # Track snapshots created before exact-title family comparison can
            # contain a perfectly valid recording for the wrong credited
            # artist. Re-evaluate those once through the current resolver.
            return True
        target_artist = dict(snapshot.get("lead_artist") or {})
        target_artist_name = normalize_artist_name(
            target_artist.get("name")
            or target_artist.get("artist")
            or (target.get("item") or {}).get("artist")
            or (target.get("item") or {}).get("name")
        )
        # Cross-type homonyms need one independent retry. For example, an
        # exact-name artist must not permanently hide an exact-title recording
        # credited to a different artist.
        cross_type_recording_conflict = bool(
            target_type in {"artist", "album"}
            and target_artist_name
            and any(
            search_text_similarity(
                query,
                str(track.get("title") or track.get("name") or ""),
            )
            >= 0.94
            and normalize_artist_name(
                track.get("channel") or track.get("artist")
            )
            not in {"", target_artist_name}
            for track in list(snapshot.get("tracks") or [])
            if isinstance(track, dict)
            )
        )
        if (
            cross_type_recording_conflict
            and attempts < _SEARCH_TARGET_REVALIDATION_MAX_ATTEMPTS
        ):
            return True
        if state == "complete" or attempts >= _SEARCH_TARGET_REVALIDATION_MAX_ATTEMPTS:
            return False
        if str(target.get("confidence_tier") or "").casefold() != "authoritative":
            return True
        if target_type not in {"artist", "album"}:
            return False
        return not target_artist_name

    def _hydrate_accepted_target_essentials(
        self,
        target: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Resolve only the accepted track's artist and containing album."""
        hydrated = deepcopy(target or {})
        if str(hydrated.get("entity_type") or "").casefold() != "track":
            return hydrated
        item = dict(hydrated.get("item") or {})
        lead_artist = dict(hydrated.get("lead_artist") or {})
        containing_album = dict(hydrated.get("containing_album") or {})
        artist_name = str(
            lead_artist.get("name")
            or lead_artist.get("artist")
            or item.get("channel")
            or item.get("artist")
            or ""
        ).strip()
        expected_album_title = str(
            containing_album.get("title")
            or containing_album.get("name")
            or containing_album.get("album")
            or item.get("album")
            or item.get("album_title")
            or ""
        ).strip()
        executor = getattr(self._server, "search_executor", None)
        if executor is None:
            return hydrated

        futures: Dict[Any, str] = {}
        if artist_name and not _artist_provider_id(lead_artist):
            futures[
                executor.submit(
                    search_artists_direct_cached,
                    artist_name,
                    8,
                    server=self._server,
                )
            ] = "artist"
        if expected_album_title and not catalog_album_is_detail_ready(
            containing_album
        ):
            album_probe = {
                **item,
                "album": expected_album_title,
                "album_title": expected_album_title,
                "channel": artist_name,
                "artist": artist_name,
            }
            futures[
                executor.submit(
                    search_canonical_album_for_track,
                    album_probe,
                    server=self._server,
                )
            ] = "album"
        if not futures:
            return hydrated

        completed, _ = wait(
            set(futures),
            timeout=_SEARCH_TARGET_ESSENTIAL_BUDGET_SECONDS,
        )
        for future in completed:
            try:
                result = future.result()
            except Exception:
                continue
            if futures[future] == "artist":
                candidates = [
                    dict(candidate)
                    for candidate in list(result or [])
                    if isinstance(candidate, dict)
                    and search_text_similarity(
                        artist_name,
                        str(
                            candidate.get("name")
                            or candidate.get("artist")
                            or ""
                        ),
                    )
                    >= 0.98
                    and _artist_provider_id(candidate)
                ]
                if candidates:
                    provider_artist = max(
                        candidates,
                        key=lambda candidate: (
                            str(candidate.get("source_authority") or "").casefold()
                            in {
                                "official",
                                "official_artist_channel",
                                "verified_catalog",
                                "ytmusic_artist_detail",
                            },
                            normalized_popularity(candidate),
                            bool(candidate.get("thumbnail")),
                        ),
                    )
                    lead_artist = _merge_artist_values(
                        lead_artist,
                        provider_artist,
                    )
            elif isinstance(result, dict) and result:
                expected_album = {
                    **containing_album,
                    "title": expected_album_title,
                    "artist": artist_name,
                }
                bound_album = _bind_containing_album_from_artist_catalog(
                    expected_album,
                    [dict(result)],
                    lead_artist,
                )
                if bound_album:
                    containing_album = bound_album

        hydrated["lead_artist"] = lead_artist
        hydrated["containing_album"] = containing_album
        return hydrated

    def _schedule_search_snapshot_completion(
        self,
        *,
        snapshot_key: str,
        query: str,
        search_mode: str,
        user_scope_id: str,
    ) -> bool:
        with _SEARCH_SNAPSHOT_COMPLETION_LOCK:
            if snapshot_key in _SEARCH_SNAPSHOT_COMPLETION_PENDING:
                return False
            _SEARCH_SNAPSHOT_COMPLETION_PENDING.add(snapshot_key)

        def run() -> None:
            try:
                snapshot = _load_search_snapshot(snapshot_key)
                if not snapshot:
                    return
                starting_revision = int(snapshot.get("revision") or 1)
                request = SimpleNamespace(
                    user_scope_id=user_scope_id or "guest",
                    result_type="artists",
                )
                revalidate_target = self._snapshot_target_needs_revalidation(
                    query,
                    snapshot,
                )
                try:
                    completed = self._expand_search_snapshot_surface(
                        req=request,
                        query=query,
                        search_mode=search_mode,
                        surface="artists",
                        snapshot=snapshot,
                        revalidate_target=revalidate_target,
                    )
                except Exception as exc:
                    completed = snapshot
                    expansion_state = dict(
                        completed.get("expansion_state") or {}
                    )
                    expansion_state["artists"] = "retryable"
                    completed["expansion_state"] = expansion_state
                    print(
                        "[EBB:search][background] "
                        f"query={query[:48]} surface=artists "
                        f"status=failed error={str(exc)[:96]}",
                        flush=True,
                    )
                with _SEARCH_SNAPSHOT_LOCK:
                    latest = deepcopy(
                        _SEARCH_SNAPSHOTS.get(snapshot_key) or {}
                    )
                    visible_before = _search_snapshot_visible_fingerprint(latest)
                    for surface in ("artists", "related_artists"):
                        completed[surface] = self._merge_snapshot_items(
                            surface,
                            list(completed.get(surface) or []),
                            list(latest.get(surface) or []),
                        )
                    latest_lead = latest.get("lead_artist")
                    completed_lead = completed.get("lead_artist")
                    if (
                        isinstance(latest_lead, dict)
                        and isinstance(completed_lead, dict)
                        and _same_artist_identity(completed_lead, latest_lead)
                    ):
                        completed["lead_artist"] = _merge_artist_values(
                            dict(completed_lead),
                            latest_lead,
                        )
                    current_revision = max(
                        starting_revision,
                        int(latest.get("revision") or 1),
                    )
                    completed["revision"] = current_revision + (
                        1
                        if _search_snapshot_visible_fingerprint(completed)
                        != visible_before
                        else 0
                    )
                    completed["expires_at"] = (
                        time.time() + _SEARCH_SNAPSHOT_TTL_SECONDS
                    )
                    _SEARCH_SNAPSHOTS[snapshot_key] = deepcopy(completed)
                    _SEARCH_SNAPSHOTS.move_to_end(snapshot_key)
                    while len(_SEARCH_SNAPSHOTS) > _SEARCH_SNAPSHOT_MAX_ENTRIES:
                        _SEARCH_SNAPSHOTS.popitem(last=False)
            finally:
                with _SEARCH_SNAPSHOT_COMPLETION_LOCK:
                    _SEARCH_SNAPSHOT_COMPLETION_PENDING.discard(snapshot_key)

        _SEARCH_ARTIST_METADATA_WRITER.submit(run)
        return True

    def _snapshot_progress_diagnostics(
        self,
        snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        lead_artist = dict(snapshot.get("lead_artist") or {})
        related_candidates = list(snapshot.get("related_artists") or [])
        visible_related = self._visible_artists(
            related_candidates,
            lead_artist,
        )
        expansion_state = dict(snapshot.get("expansion_state") or {})
        return {
            "search_snapshot_revision": int(snapshot.get("revision") or 1),
            "target_revalidation_pending": (
                str(snapshot.get("target_revalidation_state") or "").casefold()
                in {"pending", "retryable"}
            ),
            "target_revalidation_attempts": int(
                snapshot.get("target_revalidation_attempts") or 0
            ),
            "search_pending_surfaces": sorted(
                surface
                for surface, state in expansion_state.items()
                if state in {"pending", "retryable", "pending_artwork"}
            ),
            "artist_artwork_pending": self._missing_artist_artwork(snapshot),
            "related_artist_visible_count": len(visible_related),
            "related_artist_candidate_count": len(related_candidates),
        }

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
        allow_relationship_wait: bool = True,
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
            server=self._server,
        )
        local_catalog_ms = int((time.perf_counter() - local_started_at) * 1000)
        local_catalog_content_complete = (
            len(canonical_tracks) >= 24
            and len(canonical_albums) >= 8
        )
        local_artwork_count = sum(
            1
            for album in canonical_albums
            if catalog_thumbnail_url(album, entity_type="album")
        )
        local_catalog_complete = (
            local_catalog_content_complete
            and local_artwork_count >= min(8, len(canonical_albums))
        )
        canonical_albums = [
            album
            for album in canonical_albums
            if _search_album_is_publishable(album)
        ]

        # Last.fm relationship lookup does not depend on album-tracklist
        # completion. Start it now so unseen artist completion does not pay the
        # two provider latencies one after the other.
        lastfm_started_at = time.perf_counter()
        lastfm_future = None
        search_executor = getattr(self._server, "search_executor", None)
        if (
            allow_relationship_wait
            and len(related_artists) < 8
            and search_executor is not None
        ):
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
            "catalog_status": (
                "complete" if local_catalog_complete else "retryable"
            ),
            "album_tracklists_loaded": 0,
        }
        should_load_live_catalog = (
            not local_catalog_content_complete
            or (allow_relationship_wait and not local_catalog_complete)
        )
        if should_load_live_catalog:
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
        lead_artist = _ensure_verified_artist_artwork(
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
            server=self._server,
        )
        canonical_albums = [
            album
            for album in canonical_albums
            if _search_album_is_publishable(album)
        ]

        try:
            if not allow_relationship_wait:
                lastfm_related = []
                related_status = "pending"
            elif lastfm_future is not None:
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
            limit=_SEARCH_RELATED_ARTIST_VISIBLE_TARGET,
        )
        related_artwork_ms = int(
            (time.perf_counter() - artwork_started_at) * 1000
        )
        resolved_related = self._merge_snapshot_items(
            "related_artists",
            [],
            resolved_related,
        )
        visible_related = self._visible_artists(
            resolved_related,
            lead_artist,
        )
        if len(visible_related) < 6:
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
            for artist in visible_related[:8]
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
        relevant_playlists = [
            playlist
            for playlist in relevant_playlists
            if _search_playlist_is_publishable(playlist)
        ]
        provider_playlists_complete = len(relevant_playlists) >= 4
        if len(relevant_playlists) < 4 and canonical_tracks:
            stable_artist_key = (
                _artist_provider_id(lead_artist)
                or normalize_artist_name(artist_name).replace(" ", "-")
                or "artist"
            )
            generated_playlists: List[Dict[str, Any]] = []
            essentials = canonical_tracks[:32]
            essentials_artwork = next(
                (
                    catalog_thumbnail_url(track, entity_type="track")
                    for track in essentials
                    if catalog_thumbnail_url(track, entity_type="track")
                ),
                "",
            )
            if len(essentials) >= 4 and essentials_artwork:
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
                        "thumbnail": essentials_artwork,
                    }
                )
            deeper = canonical_tracks[8:40]
            deeper_artwork = next(
                (
                    catalog_thumbnail_url(track, entity_type="track")
                    for track in deeper
                    if catalog_thumbnail_url(track, entity_type="track")
                ),
                "",
            )
            if len(deeper) >= 4 and deeper_artwork:
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
                        "thumbnail": deeper_artwork,
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
                album_artwork = next(
                    (
                        catalog_thumbnail_url(track, entity_type="track")
                        for track in album_tracks
                        if catalog_thumbnail_url(
                            track,
                            entity_type="track",
                        )
                    ),
                    "",
                )
                if not album_artwork:
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
                        "thumbnail": album_artwork,
                    }
                )
                if len(generated_playlists) >= 6:
                    break
            relevant_playlists = self._merge_snapshot_items(
                "playlists",
                relevant_playlists,
                generated_playlists,
            )
        relevant_playlists = [
            attach_entity_artwork_proxy(
                self._server,
                playlist,
                entity_type="playlist",
            )
            for playlist in relevant_playlists
        ]
        relevant_playlists = [
            playlist
            for playlist in relevant_playlists
            if _search_playlist_is_publishable(playlist)
        ]
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
            "related_albums": [
                album
                for album in _artist_catalog_albums(
                    visible_related,
                    relationship="related_artist_discography",
                )
                if _search_album_is_publishable(album)
            ],
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
            "catalog_stage_timings_ms": dict(
                catalog.get("stage_timings_ms") or {}
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
        revalidate_target: bool = False,
    ) -> Dict[str, Any]:
        lead_artist = (
            {}
            if revalidate_target
            else dict(snapshot.get("lead_artist") or {})
        )
        recovered_target_identity = False
        target_revalidation_succeeded = False
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
            # Exact query results and artist catalog results are distinct
            # inventories. Enrichment must not turn one into the other.
            snapshot["tracks"] = previous_tracks
            snapshot["albums"] = previous_albums
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
                        if (
                            catalog_complete
                            and related_complete
                            and _artist_provider_id(
                                dict(snapshot.get("lead_artist") or {})
                            )
                            and not self._missing_artist_artwork(snapshot)
                        )
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
                force_refresh=revalidate_target,
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
            refreshed_target = dict(
                retrieval_payload.get("resolved_target") or {}
            )
            refreshed_target = self._hydrate_accepted_target_essentials(
                refreshed_target
            )
            current_target = dict(snapshot.get("resolved_target") or {})
            refreshed_lead = dict(refreshed_target.get("lead_artist") or {})
            current_lead = dict(snapshot.get("lead_artist") or {})
            refreshed_identity = _target_identity(refreshed_target)
            current_identity = _target_identity(current_target)
            same_target = bool(
                refreshed_identity
                and refreshed_identity == current_identity
            )
            stronger_target = bool(
                refreshed_identity
                and (
                    not current_identity
                    or _target_quality(refreshed_target)
                    > _target_quality(current_target)
                )
            )
            recording_family_correction = bool(
                refreshed_identity
                and refreshed_identity != current_identity
                and _target_has_recording_family_evidence(refreshed_target)
                and (
                    str(current_target.get("entity_type") or "").casefold()
                    in {"artist", "album", "mixed"}
                    or (
                        str(current_target.get("entity_type") or "").casefold()
                        == "track"
                        and "recording_family_comparison"
                        not in {
                            str(value or "").strip().casefold()
                            for value in list(current_target.get("evidence") or [])
                        }
                    )
                )
            )
            if same_target:
                target_revalidation_succeeded = True
                snapshot["resolved_target"] = {
                    **current_target,
                    **{
                        key: value
                        for key, value in refreshed_target.items()
                        if value not in (None, "", [], {})
                    },
                }
                if refreshed_lead and current_lead:
                    snapshot["lead_artist"] = _merge_artist_values(
                        current_lead,
                        refreshed_lead,
                    )
                elif refreshed_lead:
                    snapshot["lead_artist"] = refreshed_lead
            elif refreshed_lead and (stronger_target or recording_family_correction):
                target_revalidation_succeeded = True
                snapshot["resolved_target"] = refreshed_target
                snapshot["query_intent"] = str(
                    refreshed_target.get("entity_type") or "mixed"
                )
                snapshot["lead_artist"] = refreshed_lead
                containing_album = dict(
                    refreshed_target.get("containing_album") or {}
                )
                if containing_album:
                    snapshot["containing_album"] = containing_album
                else:
                    snapshot.pop("containing_album", None)
                # These surfaces belong to the previously selected artist and
                # cannot be merged into a corrected canonical target.
                snapshot["artist_tracks"] = []
                snapshot["artist_albums"] = []
                snapshot["related_artists"] = []
                snapshot["related_albums"] = []
                recovered_target_identity = True
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
                except Exception:
                    lastfm_related = []
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
                        self._visible_artists(
                            list(snapshot.get("related_artists") or []),
                            dict(snapshot.get("lead_artist") or {}),
                        ),
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
        if revalidate_target:
            attempts = int(snapshot.get("target_revalidation_attempts") or 0) + 1
            snapshot["target_revalidation_attempts"] = attempts
            refreshed_target = dict(snapshot.get("resolved_target") or {})
            snapshot["target_revalidation_state"] = (
                "complete"
                if (
                    target_revalidation_succeeded
                    and _target_identity(refreshed_target)
                    and (
                        str(
                            refreshed_target.get("confidence_tier") or ""
                        ).casefold()
                        == "authoritative"
                        or _target_has_recording_family_evidence(refreshed_target)
                        or attempts >= _SEARCH_TARGET_REVALIDATION_MAX_ATTEMPTS
                    )
                )
                else "retryable"
            )
        expansion_state = dict(snapshot.get("expansion_state") or {})
        if recovered_target_identity:
            # The first pass recovered the authoritative artist/recording.
            # Leave the artist/catalog surfaces retryable so the next bounded
            # pass can build that artist's works instead of declaring the
            # formerly-unresolved snapshot complete.
            expansion_state.update(
                {
                    "tracks": "retryable",
                    "artists": "retryable",
                    "albums": "retryable",
                }
            )
        elif surface == "artists" and self._missing_artist_artwork(snapshot):
            expansion_state[surface] = "pending_artwork"
            artwork_poll_attempts = dict(
                snapshot.get("artwork_poll_attempts") or {}
            )
            artwork_poll_attempts[surface] = 0
            snapshot["artwork_poll_attempts"] = artwork_poll_attempts
        else:
            expansion_state[surface] = "complete"
        snapshot["expansion_state"] = expansion_state
        return snapshot

    def _build_direct_search_response(
        self,
        *,
        req,
        trace: Dict[str, Any],
        query_intent: str,
        resolved_target: Dict[str, Any] | None,
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
        candidate_target = dict(resolved_target or {})
        candidate_type = str(
            candidate_target.get("entity_type") or "mixed"
        ).strip().lower()
        # The initial search path binds the accepted target before storing the
        # snapshot. Pagination and response serialization must not judge that
        # identity a second time against a partial page. Direct unit callers
        # can still supply an unbound concrete target and receive one binding.
        bound_target = (
            candidate_target
            if (
                candidate_type == "mixed"
                or candidate_target.get("ranked_target_validated") is True
            )
            else _materialize_resolved_target(
                candidate_target,
                tracks=tracks,
                artists=artists,
                albums=albums,
            )
        )
        bound_type = str(bound_target.get("entity_type") or "mixed")
        query_intent = bound_type
        bound_item = dict(bound_target.get("item") or {})
        top_result = (
            {"entity_type": bound_type, "item": bound_item}
            if bound_type in {"track", "artist", "album"} and bound_item
            else None
        )
        if bound_type == "mixed":
            lead_artist = None
            containing_album = None
        elif bound_type == "artist":
            lead_artist = bound_item
            containing_album = None
        else:
            expected_lead = dict(bound_target.get("lead_artist") or {})
            if not (
                expected_lead
                and isinstance(lead_artist, dict)
                and _same_artist_identity(expected_lead, lead_artist)
            ):
                lead_artist = None
            else:
                lead_artist = _merge_artist_values(expected_lead, lead_artist)
            if bound_type == "album":
                containing_album = bound_item
            else:
                rebound_album = _hydrate_containing_album_from_accepted_target(
                    dict(bound_target.get("containing_album") or {}),
                    [dict(containing_album or {})] if containing_album else [],
                    dict(lead_artist or {}),
                    server=self._server,
                )
                containing_album = rebound_album or None
                if rebound_album:
                    bound_target["containing_album"] = rebound_album
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
            "resolved_target": bound_target,
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
                "target_resolver": bound_target.get("resolver"),
                "target_identity": bound_target.get("target_identity"),
                "target_confidence": bound_target.get("confidence"),
                "target_confidence_tier": bound_target.get("confidence_tier"),
                "target_decision_margin": bound_target.get("decision_margin"),
                "target_evidence": list(bound_target.get("evidence") or []),
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
            automatic_confidence = float(
                bound_target.get("confidence") or 0.0
            ) if bound_type == entity_type else 0.0
            if automatic_confidence <= 0.0:
                automatic_confidence = search_text_similarity(
                    req.query or "",
                    str(candidate_text or ""),
                ) * 0.7
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
            # Rendering an automatic result is not user intent.  Persisted
            # query associations are written only by explicit click/open/play
            # interactions; otherwise one false resolution trains itself on
            # every repeat search.
            if write_resolution_memory:
                response["diagnostics"]["query_memory_written"] = False
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
        request_stage_timings_ms: Dict[str, int] = {}
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
            target_revalidation_pending = self._snapshot_target_needs_revalidation(
                query,
                snapshot,
            )
            if target_revalidation_pending:
                snapshot["target_revalidation_state"] = "pending"
                expansion_state["artists"] = "retryable"
                snapshot["expansion_state"] = expansion_state
                _store_search_snapshot(snapshot_key, snapshot)
            if (
                str(getattr(req, "context_surface", "") or "")
                == "interactive_search"
                and (
                    target_revalidation_pending
                    or expansion_state.get("artists")
                    in {"pending", "retryable", "pending_artwork"}
                )
            ):
                self._schedule_search_snapshot_completion(
                    snapshot_key=snapshot_key,
                    query=query,
                    search_mode=search_mode,
                    user_scope_id=req.user_scope_id or "guest",
                )
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
            snapshot_lead = dict(snapshot.get("lead_artist") or {})
            response_lead = (
                attach_cached_artist_artwork(self._server, snapshot_lead)
                if snapshot_lead and _artist_provider_id(snapshot_lead)
                else None
            )
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
                if surface_name == "artists":
                    surface_items = self._visible_artists(surface_items)
                elif surface_name == "related_artists":
                    surface_items = self._visible_artists(
                        surface_items,
                        snapshot_lead,
                    )
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
                resolved_target=dict(snapshot.get("resolved_target") or {}),
                limit=limit,
                track_model_version=track_model_version,
                tracks=paged["tracks"],
                artists=paged["artists"],
                albums=paged["albums"],
                similar_artists=paged["related_artists"],
                artist_tracks=deepcopy(snapshot.get("artist_tracks") or []),
                artist_albums=deepcopy(snapshot.get("artist_albums") or []),
                related_albums=deepcopy(snapshot.get("related_albums") or []),
                lead_artist=deepcopy(response_lead),
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
                    **self._snapshot_progress_diagnostics(snapshot),
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
        retrieval_started_at = time.perf_counter()
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
        request_stage_timings_ms["retrieval"] = int(
            (time.perf_counter() - retrieval_started_at) * 1000
        )
        query_intent = str(retrieval_payload.get("query_intent") or "mixed")
        ranking_started_at = time.perf_counter()
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
        request_stage_timings_ms["ranking"] = int(
            (time.perf_counter() - ranking_started_at) * 1000
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
        essential_target_started_at = time.perf_counter()
        resolved_target = _materialize_resolved_target(
            dict(retrieval_payload.get("resolved_target") or {}),
            tracks=tracks,
            artists=artists,
            albums=albums,
        )
        resolved_target = self._hydrate_accepted_target_essentials(
            resolved_target
        )
        request_stage_timings_ms["essential_target"] = int(
            (time.perf_counter() - essential_target_started_at) * 1000
        )
        query_intent = str(
            resolved_target.get("entity_type") or "mixed"
        ).strip().lower()
        lead_artist = dict(resolved_target.get("lead_artist") or {}) or None
        containing_album = (
            dict(resolved_target.get("containing_album") or {}) or None
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
        catalog_stage_timings_ms: Dict[str, int] = {}
        defer_side_surfaces = bool(
            getattr(req, "defer_side_surfaces", False)
        )
        artist_completion_started_at = time.perf_counter()
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
                allow_relationship_wait=(
                    str(getattr(req, "context_surface", "") or "")
                    != "interactive_search"
                ),
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
            catalog_stage_timings_ms = dict(
                completed_artist.get("catalog_stage_timings_ms") or {}
            )
            # Keep these query-scoped lists intact. `artist_tracks` and
            # `artist_albums` own the selected artist's catalog below.
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
        request_stage_timings_ms["artist_surface_completion"] = int(
            (time.perf_counter() - artist_completion_started_at) * 1000
        )
        for stage, elapsed_ms in catalog_stage_timings_ms.items():
            request_stage_timings_ms[f"artist_catalog.{stage}"] = int(
                elapsed_ms or 0
            )

        if query_intent == "track" and isinstance(lead_artist, dict):
            expected_containing_album = dict(containing_album or {})
            if not expected_containing_album:
                expected_containing_album = dict(
                    resolved_target.get("containing_album") or {}
                )
            rebound_album = _hydrate_containing_album_from_accepted_target(
                expected_containing_album,
                artist_albums,
                lead_artist,
                server=self._server,
            )
            if rebound_album:
                containing_album = rebound_album
                resolved_target["containing_album"] = rebound_album

        artwork_publication_started_at = time.perf_counter()
        tracks, albums = _repair_search_artwork(
            tracks,
            albums,
            server=self._server,
        )
        artist_tracks, artist_albums = _repair_search_artwork(
            artist_tracks,
            artist_albums,
            server=self._server,
        )
        if isinstance(containing_album, dict):
            _, repaired_containing = _repair_search_artwork(
                tracks,
                [containing_album],
                server=self._server,
            )
            containing_album = (
                repaired_containing[0]
                if (
                    repaired_containing
                    and _search_album_is_publishable(
                        repaired_containing[0]
                    )
                )
                else None
            )
        albums = [
            album for album in albums if _search_album_is_publishable(album)
        ]
        artist_albums = [
            album
            for album in artist_albums
            if _search_album_is_publishable(album)
        ]
        related_albums = [
            album
            for album in related_albums
            if _search_album_is_publishable(album)
        ]
        playlists = [
            attach_entity_artwork_proxy(
                self._server,
                playlist,
                entity_type="playlist",
            )
            for playlist in playlists
        ]
        playlists = [
            playlist
            for playlist in playlists
            if _search_playlist_is_publishable(playlist)
        ]
        request_stage_timings_ms["artwork_and_publication"] = int(
            (time.perf_counter() - artwork_publication_started_at) * 1000
        )

        snapshot = {
            "revision": 1,
            "query_intent": query_intent,
            "resolved_target": resolved_target,
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
                    if (
                        not isinstance(lead_artist, dict)
                        or catalog_status != "complete"
                    )
                    else "pending"
                    if bool(getattr(req, "defer_side_surfaces", False))
                    else "complete"
                ),
                "artists": (
                    "retryable"
                    if (
                        not isinstance(lead_artist, dict)
                        or (
                            catalog_status != "complete"
                            or related_status != "complete"
                        )
                    )
                    else "pending"
                    if bool(getattr(req, "defer_side_surfaces", False))
                    else "complete"
                ),
                "albums": (
                    "retryable"
                    if (
                        not isinstance(lead_artist, dict)
                        or catalog_status != "complete"
                    )
                    else "pending"
                    if bool(getattr(req, "defer_side_surfaces", False))
                    else "complete"
                ),
                "playlists": (
                    "retryable"
                    if (
                        not isinstance(lead_artist, dict)
                        or not playlists_complete
                    )
                    else "pending"
                    if bool(getattr(req, "defer_side_surfaces", False))
                    else "complete"
                ),
            },
        }
        if (
            isinstance(lead_artist, dict)
            and (
                not _artist_provider_id(lead_artist)
                or self._missing_artist_artwork(snapshot)
            )
        ):
            snapshot["expansion_state"]["artists"] = "retryable"
        target_revalidation_pending = self._snapshot_target_needs_revalidation(
            query,
            snapshot,
        )
        snapshot["target_revalidation_state"] = (
            "pending" if target_revalidation_pending else "complete"
        )
        snapshot["target_revalidation_attempts"] = 0
        if target_revalidation_pending:
            snapshot["expansion_state"]["artists"] = "retryable"
        snapshot_started_at = time.perf_counter()
        _store_search_snapshot(snapshot_key, snapshot)
        request_stage_timings_ms["snapshot_store"] = int(
            (time.perf_counter() - snapshot_started_at) * 1000
        )
        if (
            str(getattr(req, "context_surface", "") or "")
            == "interactive_search"
            and dict(snapshot.get("expansion_state") or {}).get("artists")
            in {"pending", "retryable", "pending_artwork"}
        ):
            self._schedule_search_snapshot_completion(
                snapshot_key=snapshot_key,
                query=query,
                search_mode=search_mode,
                user_scope_id=req.user_scope_id or "guest",
            )
        offset = request_offset
        page_size = max(8, min(int(limit or 16), 24))
        pages: Dict[str, Any] = {}
        paged_tracks, pages["tracks"] = self._surface_page(
            tracks, offset=offset if requested_surface == "tracks" else 0,
            limit=page_size,
        )
        visible_artists = self._visible_artists(artists)
        response_lead = (
            attach_cached_artist_artwork(self._server, lead_artist)
            if isinstance(lead_artist, dict) and _artist_provider_id(lead_artist)
            else None
        )
        paged_artists, pages["artists"] = self._surface_page(
            visible_artists,
            offset=offset if requested_surface == "artists" else 0,
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
        visible_related_artists = self._visible_artists(
            related_artists,
            lead_artist,
        )
        paged_related, pages["related_artists"] = self._surface_page(
            visible_related_artists,
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
        if not requested_surface:
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
                f"stages={request_stage_timings_ms} "
                f"retrieval_stages={dict((retrieval_payload.get('retrieval_diagnostics') or {}).get('stage_timings_ms') or {})} "
                f"providers={dict((retrieval_payload.get('retrieval_diagnostics') or {}).get('provider_timings_ms') or {})} "
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
            resolved_target=resolved_target,
        )
        response = self._build_direct_search_response(
            req=req,
            trace=trace,
            query_intent=query_intent,
            resolved_target=resolved_target,
            limit=limit,
            track_model_version=track_model_version,
            tracks=paged_tracks,
            artists=paged_artists,
            albums=paged_albums,
            similar_artists=paged_related,
            artist_tracks=artist_tracks,
            artist_albums=artist_albums,
            related_albums=related_albums,
            lead_artist=response_lead,
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
        diagnostics["stage_timings_ms"] = dict(request_stage_timings_ms)
        diagnostics["artist_catalog"] = {
            "status": catalog_status,
            "source": catalog_source,
            "track_count": len(artist_tracks),
            "album_count": len(artist_albums),
            "playlist_count": len(playlists),
            "related_artist_count": len(visible_related_artists),
            "album_tracklists_loaded": album_tracklists_loaded,
        }
        diagnostics["profile_build_skipped"] = True
        diagnostics["relevance_admission"] = True
        diagnostics.update(self._snapshot_progress_diagnostics(snapshot))
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
