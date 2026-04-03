from __future__ import annotations

from fastapi import APIRouter, Request

from ..legacy import get_server
from ..recommend.service import RecommendationService
from ..search.service import SearchService

router = APIRouter()

_server = get_server()
_search_service = SearchService(_server)
_recommendation_service = RecommendationService(_server)


@router.get("/")
def health_check():
    return _server.health_check()


@router.get("/latency_summary")
def latency_summary():
    return _server.latency_summary()


@router.post("/prepare_session")
def prepare_session(req: _server.WarmStreamRequest):
    return _server.prepare_session(req)


@router.post("/track_details")
def get_track_details(req: _server.DownloadRequest):
    return _server.get_track_details(req)


@router.get("/lyrics/{video_id}")
def get_track_lyrics(video_id: str):
    return _server.get_track_lyrics(video_id)


@router.post("/search")
def search(req: _server.SearchRequest):
    return _search_service.search(req)


@router.post("/search_albums")
def search_albums(req: _server.SearchRequest):
    return _search_service.search_albums(req)


@router.post("/search_artists")
def search_artists(req: _server.SearchRequest):
    return _search_service.search_artists(req)


@router.post("/recommended_artists")
def recommended_artists(req: _server.SearchRequest):
    return _recommendation_service.recommended_artists(req)


@router.post("/suggest")
def get_suggestions(req: _server.SearchRequest):
    return _search_service.suggest(req)


@router.post("/recommend")
def recommend(req: _server.SearchRequest):
    return _recommendation_service.recommend(req)


@router.post("/interaction_event")
def recommendation_interaction_event(req: _server.RecommendationInteractionEventRequest):
    return _recommendation_service.interaction_event(req)


@router.post("/search_interaction")
def recommendation_search_interaction(req: _server.RecommendationSearchEventRequest):
    return _recommendation_service.search_interaction(req)


@router.get("/recommendation_model")
def recommendation_model_status():
    return _recommendation_service.model_status()


@router.get("/recommendation_model/versions")
def recommendation_model_versions():
    return _recommendation_service.model_versions()


@router.get("/model_registry/{model_key}/versions")
def model_registry_versions(model_key: str, limit: int = 20):
    return _recommendation_service.model_registry_versions(
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
    return _recommendation_service.model_registry_activate(
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
    return _recommendation_service.model_registry_rollback(
        model_key=model_key,
        target_version=target_version,
        actor=actor,
        reason=reason,
    )


@router.get("/model_registry/rollouts")
def model_registry_rollouts(model_key: str = "", limit: int = 50):
    return _recommendation_service.model_rollout_events(
        model_key=model_key,
        limit=limit,
    )


@router.get("/recommendation_experiments")
def recommendation_experiments(window_hours: int = _server.RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS):
    return _recommendation_service.experiments(window_hours=window_hours)


@router.post("/recommendation_experiments/evaluate")
def recommendation_experiments_evaluate(
    force_promote: bool = False,
    window_hours: int = _server.RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS,
):
    return _recommendation_service.evaluate_experiments(
        force_promote=force_promote,
        window_hours=window_hours,
    )


@router.post("/recommendation_model/train")
def recommendation_model_train(req: _server.RecommendationModelTrainRequest):
    return _recommendation_service.train_model(req)


@router.get("/album/{album_id}")
def get_album_details(album_id: str):
    return _server.get_album_details(album_id)


@router.get("/artist/{artist_id}")
def get_artist_details(artist_id: str):
    return _server.get_artist_details(artist_id)


@router.get("/assistant/sessions")
def assistant_list_sessions(user_scope_id: str, include_archived: bool = False):
    return _server.assistant_list_sessions(user_scope_id, include_archived=include_archived)


@router.post("/assistant/sessions")
def assistant_create_session(req: _server.AssistantSessionCreateRequest):
    return _server.assistant_create_session(req)


@router.get("/assistant/sessions/{session_id}")
def assistant_get_session(session_id: str, user_scope_id: str):
    return _server.assistant_get_session(session_id, user_scope_id)


@router.patch("/assistant/sessions/{session_id}")
def assistant_update_session(session_id: str, req: _server.AssistantSessionUpdateRequest):
    return _server.assistant_update_session(session_id, req)


@router.delete("/assistant/sessions/{session_id}")
def assistant_delete_session(session_id: str, user_scope_id: str):
    return _server.assistant_delete_session(session_id, user_scope_id)


@router.post("/assistant/chat")
def assistant_chat(req: _server.AssistantChatRequest):
    return _server.assistant_chat(req)


@router.post("/warm_streams")
def warm_streams(req: _server.WarmStreamRequest):
    return _server.warm_streams(req)


@router.post("/download")
def download_audio(req: _server.DownloadRequest):
    return _server.download_audio(req)


@router.get("/stream/{video_id}")
def stream_audio(video_id: str):
    return _server.stream_audio(video_id)


@router.get("/proxy_stream/{video_id}")
def proxy_stream(video_id: str, request: Request):
    return _server.proxy_stream(video_id, request)


@router.get("/direct_url/{video_id}")
def direct_stream_url(video_id: str):
    return _server.direct_stream_url(video_id)
