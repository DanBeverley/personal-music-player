from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import server as server_runtime

from auralis_backend.api import configure_router, router
from auralis_backend.storage.postgres import ensure_backend_schema
from auralis_backend.api.stream_core_runtime import start_playback_tools, stop_playback_tools

app = FastAPI(title="Auralis Python Proxy API", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=['Server-Timing', 'X-Neatie-Playback-Request', 'X-Auralis-Stream-Cache'],
)
configure_router(server_runtime)
app.include_router(router)


@app.on_event("startup")
def startup() -> None:
    start_playback_tools(server_runtime)
    ensure_backend_schema()
    server_runtime.startup_recommendation_runtime()


@app.on_event("shutdown")
def shutdown() -> None:
    stop_playback_tools()
    server_runtime.shutdown_recommendation_runtime()
