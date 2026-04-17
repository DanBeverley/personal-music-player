from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import server as server_runtime

from auralis_backend.api import configure_router, router
from auralis_backend.storage.postgres import ensure_backend_schema

app = FastAPI(title="Auralis Python Proxy API", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
configure_router(server_runtime)
app.include_router(router)


@app.on_event("startup")
def startup() -> None:
    ensure_backend_schema()
    server_runtime.startup_recommendation_runtime()


@app.on_event("shutdown")
def shutdown() -> None:
    server_runtime.shutdown_recommendation_runtime()
