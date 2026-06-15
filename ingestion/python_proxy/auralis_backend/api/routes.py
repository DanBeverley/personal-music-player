from __future__ import annotations

from fastapi import APIRouter, Request

from .media_runtime import (
    MediaService,
)
from .assistant_runtime import AssistantService
from .stream_runtime import (
    direct_stream_url as direct_stream_url_runtime,
    download_audio as download_audio_runtime,
    proxy_stream as proxy_stream_runtime,
    stream_audio as stream_audio_runtime,
    warm_streams as warm_streams_runtime,
)
from ..recognition.service import RecognitionService
from ..recommend.service import RecommendationService
from ..search.service import SearchService
from ..contracts import (
    AssistantChatRequest,
    AssistantSessionCreateRequest,
    AssistantSessionUpdateRequest,
    DownloadRequest,
    HistorySeedRequest,
    RecommendationInteractionEventRequest,
    RecommendationModelTrainRequest,
    RecommendationSearchEventRequest,
    SearchRequest,
    WarmStreamRequest,
)

router = APIRouter()

_server = None
_search_service = None
_recommendation_service = None
_assistant_service = None
_media_service = None
_recognition_service = None


def configure_router(server) -> None:
    global _server
    global _search_service
    global _recommendation_service
    global _assistant_service
    global _media_service
    global _recognition_service
    _server = server
    _search_service = SearchService(server)
    _recommendation_service = RecommendationService(server)
    _assistant_service = AssistantService(server)
    _media_service = MediaService(server)
    _recognition_service = RecognitionService(server)


def _require_server():
    if _server is None:
        raise RuntimeError("Router server runtime has not been configured")
    return _server


def _require_search_service() -> SearchService:
    if _search_service is None:
        raise RuntimeError("Search service has not been configured")
    return _search_service


def _require_recommendation_service() -> RecommendationService:
    if _recommendation_service is None:
        raise RuntimeError("Recommendation service has not been configured")
    return _recommendation_service


def _require_assistant_service() -> AssistantService:
    if _assistant_service is None:
        raise RuntimeError("Assistant service has not been configured")
    return _assistant_service


def _require_media_service() -> MediaService:
    if _media_service is None:
        raise RuntimeError("Media service has not been configured")
    return _media_service


def _require_recognition_service() -> RecognitionService:
    if _recognition_service is None:
        raise RuntimeError("Recognition service has not been configured")
    return _recognition_service


@router.get("/")
def health_check():
    return _require_media_service().health_check()


@router.get("/latency_summary")
def latency_summary():
    return _require_media_service().latency_summary()


@router.post("/prepare_session")
def prepare_session(req: WarmStreamRequest):
    return _require_media_service().prepare_session(req)


@router.post("/track_details")
def get_track_details(req: DownloadRequest):
    return _require_media_service().get_track_details(req)


@router.get("/lyrics/{video_id}")
def get_track_lyrics(video_id: str):
    return _require_media_service().get_track_lyrics(video_id)


@router.post("/search")
def search(req: SearchRequest):
    return _require_search_service().search(req)


@router.post("/recognize_audio")
async def recognize_audio(request: Request):
    return await _require_recognition_service().recognize_audio(request)


@router.post("/search_albums")
def search_albums(req: SearchRequest):
    return _require_search_service().search_albums(req)


@router.post("/search_artists")
def search_artists(req: SearchRequest):
    return _require_search_service().search_artists(req)


@router.post("/resolve_artist")
def resolve_artist(req: SearchRequest):
    return _require_search_service().resolve_artist(req)


@router.post("/recommended_artists")
def recommended_artists(req: SearchRequest):
    return _require_recommendation_service().recommended_artists(req)


@router.post("/suggest")
def get_suggestions(req: SearchRequest):
    return _require_search_service().suggest(req)


@router.post("/recommend")
def recommend(req: SearchRequest):
    return _require_recommendation_service().recommend(req)


@router.post("/interaction_event")
def recommendation_interaction_event(req: RecommendationInteractionEventRequest):
    return _require_recommendation_service().interaction_event(req)


@router.post("/search_interaction")
def recommendation_search_interaction(req: RecommendationSearchEventRequest):
    return _require_recommendation_service().search_interaction(req)


@router.post("/history_seed")
def recommendation_history_seed(req: HistorySeedRequest):
    return _require_recommendation_service().history_seed(req)


@router.get("/recommendation_model")
def recommendation_model_status():
    return _require_recommendation_service().model_status()


@router.get("/recommendation_model/versions")
def recommendation_model_versions():
    return _require_recommendation_service().model_versions()


@router.get("/model_registry/{model_key}/versions")
def model_registry_versions(model_key: str, limit: int = 20):
    return _require_recommendation_service().model_registry_versions(
        model_key=model_key,
        limit=limit,
    )


@router.post("/model_registry/{model_key}/activate")
def model_registry_activate(
    model_key: str,
    version: str,
    actor: str = "system",
    reason: str = "",
):
    return _require_recommendation_service().model_registry_activate(
        model_key=model_key,
        version=version,
        actor=actor,
        reason=reason,
    )


@router.post("/model_registry/{model_key}/rollback")
def model_registry_rollback(
    model_key: str,
    target_version: str = "",
    actor: str = "system",
    reason: str = "",
):
    return _require_recommendation_service().model_registry_rollback(
        model_key=model_key,
        target_version=target_version,
        actor=actor,
        reason=reason,
    )


@router.get("/model_registry/rollouts")
def model_registry_rollouts(model_key: str = "", limit: int = 50):
    return _require_recommendation_service().model_rollout_events(
        model_key=model_key,
        limit=limit,
    )


@router.get("/recommendation_experiments")
def recommendation_experiments(window_hours: int | None = None):
    server = _require_server()
    resolved_window_hours = int(
        window_hours or server.RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS
    )
    return _require_recommendation_service().experiments(window_hours=resolved_window_hours)


@router.post("/recommendation_experiments/evaluate")
def recommendation_experiments_evaluate(
    force_promote: bool = False,
    window_hours: int | None = None,
):
    server = _require_server()
    resolved_window_hours = int(
        window_hours or server.RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS
    )
    return _require_recommendation_service().evaluate_experiments(
        force_promote=force_promote,
        window_hours=resolved_window_hours,
    )


@router.post("/recommendation_model/train")
def recommendation_model_train(req: RecommendationModelTrainRequest):
    return _require_recommendation_service().train_model(req)


@router.get("/album/{album_id}")
def get_album_details(album_id: str):
    return _require_media_service().get_album_details(album_id)


@router.get("/artist/{artist_id}")
def get_artist_details(artist_id: str):
    return _require_media_service().get_artist_details(artist_id)


@router.get("/assistant/sessions")
def assistant_list_sessions(user_scope_id: str, include_archived: bool = False):
    return _require_assistant_service().list_sessions(
        user_scope_id,
        include_archived=include_archived,
    )


@router.post("/assistant/sessions")
def assistant_create_session(req: AssistantSessionCreateRequest):
    return _require_assistant_service().create_session(req)


@router.get("/assistant/sessions/{session_id}")
def assistant_get_session(session_id: str, user_scope_id: str):
    return _require_assistant_service().get_session(session_id, user_scope_id)


@router.patch("/assistant/sessions/{session_id}")
def assistant_update_session(session_id: str, req: AssistantSessionUpdateRequest):
    return _require_assistant_service().update_session(session_id, req)


@router.delete("/assistant/sessions/{session_id}")
def assistant_delete_session(session_id: str, user_scope_id: str):
    return _require_assistant_service().delete_session(session_id, user_scope_id)


@router.post("/assistant/chat")
def assistant_chat(req: AssistantChatRequest):
    return _require_assistant_service().chat(req)


@router.post("/warm_streams")
def warm_streams(req: WarmStreamRequest):
    return warm_streams_runtime(_require_server(), req)


@router.post("/download")
def download_audio(req: DownloadRequest):
    return download_audio_runtime(_require_server(), req)


@router.get("/stream/{video_id}")
def stream_audio(video_id: str):
    return stream_audio_runtime(_require_server(), video_id)


@router.get("/proxy_stream/{video_id}")
def proxy_stream(video_id: str, request: Request):
    return proxy_stream_runtime(_require_server(), video_id, request)


@router.get("/direct_url/{video_id}")
def direct_stream_url(video_id: str):
    return direct_stream_url_runtime(_require_server(), video_id)
