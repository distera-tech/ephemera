"""FastAPI application (control-plane API). Never talks to GPUs or the model server."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.config import Settings, get_settings
from app.core.container import build_database, build_workspace
from app.core.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.secret_values())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = build_database(settings)
        workspace = build_workspace(settings)
        workspace.ensure_root()
        app.state.settings = settings
        app.state.engine = db.engine
        app.state.repo = db.repo
        app.state.workspace = workspace
        try:
            yield
        finally:
            await db.engine.dispose()

    app = FastAPI(
        title="Ephemera API",
        version="0.1.0",
        description="Ephemeral AI inference orchestrator — provision, infer, clean, destroy.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
