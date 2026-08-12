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


class SearchRequest(BaseModel):
    query: str = ""
    user_scope_id: str = "guest"
    session_id: Optional[str] = None
    force_refresh: bool = False
    prepare_next_session: bool = False
    prefer_fresh_rows: bool = False
    session_intent: bool = False
    refresh_token: str = ""
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
    limit: int = 24
    context_surface: str = "search"
    surface: str = "home_feed"
    anchor_track_id: Optional[str] = None
    anchor_artist_id: Optional[str] = None
    anchor_track_snapshot: Optional[Dict[str, Any]] = None
    search_mode: str = ""
    seed_id: Optional[str] = None
    seed_ids: List[str] = Field(default_factory=list)
    anchor_artist_hints: List[str] = Field(default_factory=list)
    offset: int = 0
    result_type: str = ""
    cursor: str = ""
    row_id: Optional[str] = None
    row_context: Optional[str] = None
    defer_side_surfaces: bool = False
    # Optional bounded long-poll for an existing canonical search snapshot.
    # The backend never starts retrieval for a revision wait.
    revision: int = 0
    revision_wait_ms: int = 0
    recent_track_snapshots: List[Dict[str, Any]] = Field(default_factory=list)
    top_track_snapshots: List[Dict[str, Any]] = Field(default_factory=list)
    anchor_track_snapshots: List[Dict[str, Any]] = Field(default_factory=list)
    client_signal_tier: str = ""
    fresh_account_empty_home: bool = False


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


class HistorySeedRequest(BaseModel):
    user_scope_id: str = "guest"
    limit: int = 24


class RecommendationModelTrainRequest(BaseModel):
    force_sync: bool = False


class RecommendationPreferencesRequest(BaseModel):
    user_scope_id: str = "guest"
    taste_mode: str = "neatie"
    listenbrainz_username: str = ""


class DownloadRequest(BaseModel):
    video_id: str
    title: str = ""


class PrepareSessionRequest(BaseModel):
    track_keys: List[str] = Field(default_factory=list)
    current_track_key: Optional[str] = None
    active_queue: bool = False
    lookahead: int = 0
    background: bool = False


class PlaybackResolveRequest(BaseModel):
    track_key: str


class LyricsMeaningRequest(BaseModel):
    video_id: str
    title: str = ""
    artist: str = ""
    album: Optional[str] = None
    year: Optional[str] = None
    source: Optional[str] = None
    lines: List[Dict[str, Any]] = Field(default_factory=list)
    user_scope_id: str = "guest"


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
    similar_tracks: List[Dict[str, Any]] = Field(default_factory=list)
    artist_tracks: List[Dict[str, Any]] = Field(default_factory=list)
    related_albums: List[Dict[str, Any]] = Field(default_factory=list)
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


class RecognitionResolvedTrackCandidate(BaseModel):
    recognized_metadata: Dict[str, Any] = Field(default_factory=dict)
    resolved_track: Optional[Dict[str, Any]] = None
    confidence: float = 0.0
    resolution_score: float = 0.0
    window_hits: int = 0


class RecognitionAudioResponse(BaseModel):
    status: str = "success"
    request_id: str
    recognition_status: str
    provider: str = ""
    confidence: float = 0.0
    recognized_metadata: Optional[Dict[str, Any]] = None
    resolved_track: Optional[Dict[str, Any]] = None
    alternatives: List[Dict[str, Any]] = Field(default_factory=list)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
