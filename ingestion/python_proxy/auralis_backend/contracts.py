from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TrackSnapshot(BaseModel):
    id: Optional[str] = None
    title: Optional[str] = None
    channel: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    album_id: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: Optional[int] = None


class SearchV2Request(BaseModel):
    query: str
    user_scope_id: str = "guest"
    session_id: Optional[str] = None
    limit: int = 24
    recent_queries: List[str] = Field(default_factory=list)
    recent_tracks: List[Dict[str, Any]] = Field(default_factory=list)
    context_surface: str = "search"
    force_refresh: bool = False


class SuggestV2Request(BaseModel):
    query: str
    user_scope_id: str = "guest"
    session_id: Optional[str] = None
    limit: int = 6
    recent_queries: List[str] = Field(default_factory=list)
    recent_tracks: List[Dict[str, Any]] = Field(default_factory=list)


class RecommendationHomeV2Request(BaseModel):
    query: str = ""
    user_scope_id: str = "guest"
    session_id: Optional[str] = None
    force_refresh: bool = False
    prefer_fresh_rows: bool = False
    refresh_token: str = ""
    hydrate_heavy_rows: bool = False
    recent_track_ids: List[str] = Field(default_factory=list)
    top_track_ids: List[str] = Field(default_factory=list)
    recent_tracks: List[Dict[str, Any]] = Field(default_factory=list)
    top_tracks: List[Dict[str, Any]] = Field(default_factory=list)
    last_played_tracks: List[Dict[str, Any]] = Field(default_factory=list)
    recent_queries: List[str] = Field(default_factory=list)
    taste_queries: List[str] = Field(default_factory=list)
    artist_hints: List[str] = Field(default_factory=list)
    album_hints: List[str] = Field(default_factory=list)
    playlist_names: List[str] = Field(default_factory=list)
    library_track_ids: List[str] = Field(default_factory=list)
    offline_track_ids: List[str] = Field(default_factory=list)
    avoid_ids: List[str] = Field(default_factory=list)
    limit: int = 18


class RecommendationRowPageV2Request(BaseModel):
    user_scope_id: str = "guest"
    session_id: str
    row_id: str
    offset: int = 0
    limit: int = 8


class SimilarArtistsV2Request(BaseModel):
    user_scope_id: str = "guest"
    session_id: Optional[str] = None
    query: str = ""
    surface: str = "search_results"
    anchor_track_id: Optional[str] = None
    anchor_artist_id: Optional[str] = None
    anchor_track_snapshot: Optional[Dict[str, Any]] = None
    recent_queries: List[str] = Field(default_factory=list)
    recent_tracks: List[Dict[str, Any]] = Field(default_factory=list)
    limit: int = 8


class SearchV3Request(SearchV2Request):
    recent_track_ids: List[str] = Field(default_factory=list)
    top_track_ids: List[str] = Field(default_factory=list)
    top_tracks: List[Dict[str, Any]] = Field(default_factory=list)
    last_played_tracks: List[Dict[str, Any]] = Field(default_factory=list)
    taste_queries: List[str] = Field(default_factory=list)
    artist_hints: List[str] = Field(default_factory=list)
    album_hints: List[str] = Field(default_factory=list)
    playlist_names: List[str] = Field(default_factory=list)
    library_track_ids: List[str] = Field(default_factory=list)
    offline_track_ids: List[str] = Field(default_factory=list)


class RecommendationHomeV3Request(RecommendationHomeV2Request):
    pass


class RecommendationRowPageV3Request(RecommendationRowPageV2Request):
    pass


class SimilarArtistsV3Request(SimilarArtistsV2Request):
    pass


class SearchTopResult(BaseModel):
    entity_type: str
    item: Dict[str, Any]


class RecommendationShelf(BaseModel):
    id: str
    title: str
    kind: str
    item_type: str
    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_offset: int = 0
    has_more: bool = False


class SearchPageResponse(BaseModel):
    status: str = "success"
    request_id: str
    model_version: str
    query_intent: str
    top_result: Optional[SearchTopResult] = None
    tracks: List[Dict[str, Any]] = Field(default_factory=list)
    artists: List[Dict[str, Any]] = Field(default_factory=list)
    albums: List[Dict[str, Any]] = Field(default_factory=list)
    similar_artists: List[Dict[str, Any]] = Field(default_factory=list)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)


class RecommendationHomeResponse(BaseModel):
    status: str = "success"
    request_id: str
    session_id: str
    generated_at: Optional[float] = None
    expires_at: Optional[float] = None
    model_version: str
    rows: List[RecommendationShelf] = Field(default_factory=list)
    shelves: List[RecommendationShelf] = Field(default_factory=list)
    recommendations: List[Dict[str, Any]] = Field(default_factory=list)
    has_more: bool = False
    next_offset: int = 0
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
