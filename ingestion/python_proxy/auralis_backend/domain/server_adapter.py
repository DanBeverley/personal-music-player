from __future__ import annotations

from typing import Any, Iterable, Optional

from ..runtime_context import resolve_server


class DomainServerAdapter:
    def __init__(self, server: Any | None = None) -> None:
        self.raw = resolve_server(server)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)

    def trim_text(self, value: Optional[str]) -> str:
        return self.raw._recommendation_trim_text(value)

    def unique_strings(
        self,
        values: Iterable[str] | None,
        limit: int | None = None,
    ) -> list[str]:
        return list(self.raw._recommendation_unique_strings(values or [], limit))

    def normalize_text(self, value: Any) -> str:
        return self.raw._normalize_text(value)

    def unique_snapshot_tracks(self, tracks, limit: int):
        return self.raw._recommendation_unique_snapshot_tracks(tracks, limit)

    def unique_track_ids(self, track_ids, limit: int):
        return self.raw._recommendation_unique_track_ids(track_ids, limit)

    def normalize_track(self, track: Any):
        return self.raw.normalize_recommendation_track(track)

    def recommendation_track_signature(self, track: Any) -> str:
        return self.raw._recommendation_track_signature(track)

    def recommendation_fetch_tracks_for_ids(self, track_ids, limit: int):
        return self.raw._recommendation_fetch_tracks_for_ids(track_ids, limit)

    def assistant_tool_get_similar_tracks(self, track_id: str, limit: int):
        return self.raw._assistant_tool_get_similar_tracks(track_id, limit)

    def build_artist_details_payload(
        self,
        artist_id: str,
        *,
        enrich_related: bool = True,
        lightweight: bool = False,
    ):
        return self.raw._build_artist_details_payload(
            artist_id,
            enrich_related=enrich_related,
            lightweight=lightweight,
        )

    def assistant_tool_search_albums(self, query: str, limit: int):
        return self.raw._assistant_tool_search_albums(query, limit)

    def assistant_tool_search_tracks(self, query: str, limit: int):
        return self.raw._assistant_tool_search_tracks(query, limit)

    def assistant_tool_get_album_details(self, album_id: str):
        return self.raw._assistant_tool_get_album_details(album_id)

    def recommendation_collaborative_neighbor_tracks(self, track_id: str, limit: int):
        return self.raw._recommendation_collaborative_neighbor_tracks(track_id, limit)

    def build_search_request(self, **kwargs):
        return self.raw.SearchRequest(**kwargs)

    def merge_track_metadata(self, raw_track, normalized_track):
        return self.raw._merge_track_metadata(raw_track, normalized_track)

    def recommendation_assignment_for_user(self, user_scope_id: str) -> str:
        return self.raw._recommendation_assignment_for_user(user_scope_id)

    def recommendation_track_embeddings(self, items):
        return self.raw._recommendation_track_embeddings(items)

    def recommendation_track_embedding_key(self, track: Any) -> str:
        return self.raw._recommendation_track_embedding_key(track)

    def recommendation_artist_embeddings(self, items):
        return self.raw._recommendation_artist_embeddings(items)

    def recommendation_artist_embedding_key(self, artist: Any) -> str:
        return self.raw._recommendation_artist_embedding_key(artist)

    def recommendation_text_embedding_key(self, namespace: str, text: str) -> str:
        return self.raw._recommendation_text_embedding_key(namespace, text)

    def recommendation_embed_entries(self, kind: str, entries):
        return self.raw._recommendation_embed_entries(kind, entries)

    def vector_weighted_average(self, items):
        return self.raw._vector_weighted_average(items)

    def artist_related_name_penalty(self, left: str, right: str) -> float:
        return self.raw._artist_related_name_penalty(left, right)

    def cosine_similarity(self, left, right) -> float:
        return self.raw._assistant_cosine_similarity(left, right)


def adapt_domain_server(server: Any | None = None) -> DomainServerAdapter:
    if isinstance(server, DomainServerAdapter):
        return server
    return DomainServerAdapter(server)
