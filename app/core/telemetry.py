"""Logs estruturados e tracing (RNF-08).

Cada log carrega `tenant_id` e `conversation_id` quando disponiveis, para que o trace
por conversa da secao 17.1 seja reconstruivel.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog
from fastapi import FastAPI

from app.core.config import Settings

_tenant_id: ContextVar[str | None] = ContextVar("tenant_id", default=None)
_conversation_id: ContextVar[str | None] = ContextVar("conversation_id", default=None)


def _inject_context(_logger: object, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    tenant = _tenant_id.get()
    conversation = _conversation_id.get()
    if tenant is not None:
        event_dict.setdefault("tenant_id", tenant)
    if conversation is not None:
        event_dict.setdefault("conversation_id", conversation)
    return event_dict


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    renderer: structlog.typing.Processor = (
        structlog.dev.ConsoleRenderer()
        if settings.environment == "local"
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _inject_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


@contextmanager
def log_context(
    *, tenant_id: str | None = None, conversation_id: str | None = None
) -> Iterator[None]:
    """Amarra tenant e conversa a todos os logs emitidos dentro do bloco."""
    tenant_token = _tenant_id.set(tenant_id) if tenant_id is not None else None
    conv_token = _conversation_id.set(conversation_id) if conversation_id is not None else None
    try:
        yield
    finally:
        if tenant_token is not None:
            _tenant_id.reset(tenant_token)
        if conv_token is not None:
            _conversation_id.reset(conv_token)


def setup_tracing(app: FastAPI, settings: Settings) -> None:
    """Instrumenta a app com OpenTelemetry quando habilitado.

    Desligado por padrao no ambiente local para nao exigir coletor rodando.
    """
    if not settings.otel_enabled:
        return

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
