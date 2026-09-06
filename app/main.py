"""Entrypoint da API (RNF-02).

O webhook de ingestao (S04) e montado aqui e nunca depende do LLM: valida, persiste,
responde 200 e enfileira.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.api.webhooks.whatsapp import router as whatsapp_webhook_router
from app.core.config import Settings, get_settings
from app.core.telemetry import configure_logging, get_logger, setup_tracing
from app.workers.queue import ArqInboundQueue

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    log.info("startup", environment=settings.environment)
    yield
    await app.state.inbound_queue.close()
    log.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.check_production_ready()
    configure_logging(settings)

    app = FastAPI(
        title="Datamind Agenda AI",
        version="0.1.0",
        docs_url="/docs" if settings.environment != "production" else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.inbound_queue = ArqInboundQueue(settings)
    setup_tracing(app, settings)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "environment": settings.environment}

    app.include_router(whatsapp_webhook_router)

    return app


app = create_app()
