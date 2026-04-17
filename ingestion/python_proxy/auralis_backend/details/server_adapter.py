from __future__ import annotations

from typing import Any, Optional

from ..runtime_context import resolve_server


class DetailServerAdapter:
    def __init__(self, server: Any | None = None) -> None:
        self.raw = resolve_server(server)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)

    def trim_text(self, value: Optional[str]) -> str:
        return self.raw._recommendation_trim_text(value)

    def cache_lookup(self, namespace: str, key: str):
        return self.raw._cache_lookup(
            self.raw.detail_result_cache,
            self.raw.detail_result_cache_lock,
            namespace,
            key,
        )

    def cache_store(self, namespace: str, key: str, payload) -> None:
        self.raw._cache_store(
            self.raw.detail_result_cache,
            self.raw.detail_result_cache_lock,
            namespace,
            key,
            payload,
            self.raw.DETAIL_RESULT_CACHE_TTL_SECONDS,
        )

    def normalize_artist_song_entries(self, entries, *, fallback_artist: str):
        return self.raw._normalize_artist_song_entries(
            entries,
            fallback_artist=fallback_artist,
        )

    def normalize_artist_album_entries(self, entries, *, fallback_artist: str):
        return self.raw._normalize_artist_album_entries(
            entries,
            fallback_artist=fallback_artist,
        )

    def rank_artist_detail_related_artists(
        self,
        artist,
        top_songs,
        related_artists,
        *,
        enrich_related: bool,
    ):
        return self.raw._rank_artist_detail_related_artists(
            artist,
            top_songs,
            related_artists,
            enrich_related=enrich_related,
        )

    def summarize_artist_description(self, description: str) -> str:
        return self.raw._summarize_artist_description(description)

    def normalize_artist_stats(self, artist) -> dict[str, Any]:
        return self.raw._normalize_artist_stats(artist)


def adapt_detail_server(server: Any | None = None) -> DetailServerAdapter:
    if isinstance(server, DetailServerAdapter):
        return server
    return DetailServerAdapter(server)
