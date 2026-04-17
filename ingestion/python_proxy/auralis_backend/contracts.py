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
    defer_side_surfaces: bool = False


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
    prepare_next_session: bool = False
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
    row_context: Optional[str] = None
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


class SearchRequest(SearchV3Request):
    surface: str = "home_feed"
    seed_id: Optional[str] = None
    seed_ids: List[str] = Field(default_factory=list)
    anchor_artist_hints: List[str] = Field(default_factory=list)
    avoid_ids: List[str] = Field(default_factory=list)
    offset: int = 0
    row_id: Optional[str] = None
    row_context: Optional[str] = None
    prepare_next_session: bool = False
    prefer_fresh_rows: bool = False
    refresh_token: str = ""
    hydrate_heavy_rows: bool = False
    defer_side_surfaces: bool = False
    recent_track_snapshots: List[Dict[str, Any]] = Field(default_factory=list)
    top_track_snapshots: List[Dict[str, Any]] = Field(default_factory=list)
    anchor_track_snapshots: List[Dict[str, Any]] = Field(default_factory=list)


class RecommendationInteractionEventRequest(BaseModel):
    user_scope_id: str = "guest"
    track_id: str
    event_type: str = "play"
    artist_name: Optional[str] = None
    source: str = "app"
    occurred_at: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RecommendationSearchEventRequest(BaseModel):
    user_scope_id: str = "guest"
    query: str
    result_count: int = 0
    source: str = "app"
    occurred_at: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RecommendationModelTrainRequest(BaseModel):
    force_sync: bool = False


class DownloadRequest(BaseModel):
    video_id: str
    title: str = ""


class WarmStreamRequest(BaseModel):
    video_ids: List[str] = Field(default_factory=list)
    current_video_id: Optional[str] = None
    active_queue: bool = False
    lookahead: int = 0


class AssistantConversationMessage(BaseModel):
    role: str
    content: str


class AssistantPlaylistSummary(BaseModel):
    id: str
    name: str
    track_count: int = 0


class AssistantLibraryTrack(BaseModel):
    id: Optional[str] = None
    title: str = ""
    artist: str = ""
    album: Optional[str] = None


class AssistantContextTrack(BaseModel):
    id: Optional[str] = None
    title: str = ""
    artist: str = ""
    channel: Optional[str] = None
    album: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: int = 0
    reason: Optional[str] = None


class AssistantChatRequest(BaseModel):
    message: str
    user_scope_id: str = "guest"
    session_id: Optional[str] = None
    thinking_mode: bool = True
    force_refresh: bool = False
    conversation: List[AssistantConversationMessage] = Field(default_factory=list)
    last_assistant_tracks: List[AssistantContextTrack] = Field(default_factory=list)
    last_playlist_draft_tracks: List[AssistantContextTrack] = Field(default_factory=list)
    recent_assistant_tracks: List[AssistantContextTrack] = Field(default_factory=list)
    playlist_summaries: List[AssistantPlaylistSummary] = Field(default_factory=list)
    recent_track_ids: List[str] = Field(default_factory=list)
    recent_queries: List[str] = Field(default_factory=list)
    library_tracks: List[AssistantLibraryTrack] = Field(default_factory=list)
    limit: int = 10


class AssistantSessionCreateRequest(BaseModel):
    user_scope_id: str = "guest"
    title: Optional[str] = None


class AssistantSessionUpdateRequest(BaseModel):
    user_scope_id: str = "guest"
    title: Optional[str] = None
    archived: Optional[bool] = None
    pinned: Optional[bool] = None


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
