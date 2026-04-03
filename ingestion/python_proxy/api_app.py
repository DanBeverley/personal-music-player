from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from auralis_backend.api import router
from auralis_backend.storage.postgres import ensure_backend_schema
from auralis_backend.legacy import get_server

app = FastAPI(title="Auralis Python Proxy API", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.on_event("startup")
def startup() -> None:
    ensure_backend_schema()
    get_server().startup_recommendation_runtime()


@app.on_event("shutdown")
def shutdown() -> None:
    get_server().shutdown_recommendation_runtime()
