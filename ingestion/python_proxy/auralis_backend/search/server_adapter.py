from __future__ import annotations

from typing import Any, Iterable, Optional

from ..runtime_context import resolve_server


class SearchServerAdapter:
    def __init__(self, server: Any | None = None) -> None:
        self.raw = resolve_server(server)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)

    def normalize_text(self, value: Any) -> str:
        return self.raw._normalize_text(value)

    def query_tokens(self, value: str | None) -> list[str]:
        return self.raw._query_tokens(value)

    def trim_text(self, value: Optional[str]) -> str:
        return self.raw._recommendation_trim_text(value)

    def unique_strings(
        self,
        values: Iterable[str] | None,
        limit: int | None = None,
    ) -> list[str]:
        return list(self.raw._recommendation_unique_strings(values or [], limit))

    def build_artist_details_payload(
        self,
        artist_id: str,
        *,
        enrich_related: bool = True,
        lightweight: bool = False,
    ) -> dict[str, Any]:
        return self.raw._build_artist_details_payload(
            artist_id,
            enrich_related=enrich_related,
            lightweight=lightweight,
        )

    def search_upstream_call_with_retry(self, fn, *, default):
        return self.raw._search_upstream_call_with_retry(
            fn,
            attempts=self.raw.UPSTREAM_RETRY_ATTEMPTS,
            backoff_seconds=self.raw.UPSTREAM_RETRY_BACKOFF_SECONDS,
            default=default,
        )

    def upstream_call_with_retry(self, fn, *, default):
        return self.raw._upstream_call_with_retry(
            fn,
            attempts=self.raw.UPSTREAM_RETRY_ATTEMPTS,
            backoff_seconds=self.raw.UPSTREAM_RETRY_BACKOFF_SECONDS,
            default=default,
        )

    def normalize_track(self, track: Any) -> dict[str, Any] | None:
        return self.raw.normalize_recommendation_track(track)

    def normalize_album_results(self, results: Any) -> list[dict[str, Any]]:
        return self.raw.normalize_album_results(results)

    def normalize_artist_results(self, results: Any) -> list[dict[str, Any]]:
        return self.raw.normalize_artist_results(results)

    def ranking_model_version(self, name: str) -> str:
        return self.raw._ranking_model_version(name)

    def trace_start(self, *args, **kwargs) -> dict[str, Any]:
        return self.raw._trace_start(*args, **kwargs)

    def trace_stage(self, trace: dict[str, Any], name: str, started_at: float) -> None:
        self.raw._trace_stage(trace, name, started_at)

    def trace_put(self, trace: dict[str, Any], category: str, key: str, value: Any) -> None:
        self.raw._trace_put(trace, category, key, value)

    def trace_finalize(self, trace: dict[str, Any], *, status: str, error: str | None = None):
        return self.raw._trace_finalize(trace, status=status, error=error)

    def trace_diagnostics(self, trace: dict[str, Any]) -> dict[str, Any]:
        return self.raw._trace_diagnostics(trace)

    def success_diagnostics(self, trace: dict[str, Any]) -> dict[str, Any]:
        return self.trace_diagnostics(self.trace_finalize(trace, status="success"))

    def trace_log_request(
        self,
        trace: dict[str, Any],
        *,
        request_type: str,
        user_scope_id: str,
        model_version: str,
    ) -> None:
        self.raw._trace_log_request(
            trace,
            request_type=request_type,
            user_scope_id=user_scope_id,
            model_version=model_version,
        )

    def assistant_tool_get_similar_tracks(
        self,
        track_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        return self.raw._assistant_tool_get_similar_tracks(track_id, limit)

    def recommendation_profile_key(self, req) -> str:
        return self.raw._recommendation_profile_key(req)

    def recommendation_unique_snapshot_tracks(self, tracks, limit: int):
        return self.raw._recommendation_unique_snapshot_tracks(tracks, limit)

    def recommendation_track_signature(self, track: Any) -> str:
        return self.raw._recommendation_track_signature(track)

    def recommendation_track_embeddings(self, items):
        return self.raw._recommendation_track_embeddings(items)

    def recommendation_track_embedding_key(self, track: Any) -> str:
        return self.raw._recommendation_track_embedding_key(track)

    def recommendation_artist_embeddings(self, items):
        return self.raw._recommendation_artist_embeddings(items)

    def recommendation_artist_embedding_key(self, artist: Any) -> str:
        return self.raw._recommendation_artist_embedding_key(artist)

    def recommendation_album_embeddings(self, items):
        return self.raw._recommendation_album_embeddings(items)

    def recommendation_album_embedding_key(self, album: Any) -> str:
        return self.raw._recommendation_album_embedding_key(album)

    def recommendation_text_embedding_key(self, namespace: str, text: str) -> str:
        return self.raw._recommendation_text_embedding_key(namespace, text)

    def recommendation_embed_entries(self, namespace_or_entries, entries=None):
        if entries is None:
            return self.raw._recommendation_embed_entries("text", namespace_or_entries)
        return self.raw._recommendation_embed_entries(namespace_or_entries, entries)

    def vector_weighted_average(self, vectors, weights=None):
        if weights is None:
            return self.raw._vector_weighted_average(vectors)
        return self.raw._vector_weighted_average(list(zip(vectors, weights)))

    def cosine_similarity(self, left, right) -> float:
        return self.raw._assistant_cosine_similarity(left, right)

    def ranking_score_features(self, *, model_key: str, defaults, features) -> float:
        return self.raw._ranking_score_features(
            model_key=model_key,
            defaults=defaults,
            features=features,
        )

    def safe_scope_id(self, value: Optional[str]) -> str:
        return self.raw._assistant_safe_scope_id(value)


def adapt_search_server(server: Any | None = None) -> SearchServerAdapter:
    if isinstance(server, SearchServerAdapter):
        return server
    return SearchServerAdapter(server)
