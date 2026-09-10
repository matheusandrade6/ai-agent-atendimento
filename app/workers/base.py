"""Contexto compartilhado pelos workers do arq.

O arq entrega a cada job um `ctx: dict` com o que o `on_startup` deixou ali. Este
modulo e quem monta esse dicionario — uma conexao de Redis (a que o proprio arq abriu),
um cliente HTTP e os dois objetos de estado efemero (agregador e rate limit) — e quem
o desmonta no fim.

Nenhum job constroi infraestrutura sozinho: quem precisa de Redis, HTTP ou banco pega
do `ctx`. E o que torna os jobs testaveis sem worker rodando — o teste monta um `ctx`
com fakes e chama a funcao.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.channels.base import OutboundChannel
from app.channels.whatsapp import WhatsAppSender
from app.core.config import Settings, get_settings
from app.core.db import dispose_engine
from app.core.telemetry import configure_logging, get_logger
from app.domain.tenant_config import TenantConfig
from app.knowledge.embeddings import EmbeddingProvider
from app.workers.aggregator import ConversationAggregator
from app.workers.queue import ArqOutboundQueue
from app.workers.ratelimit import ContactRateLimiter

log = get_logger(__name__)

WorkerContext = dict[Any, Any]


async def on_startup(ctx: WorkerContext) -> None:
    settings = get_settings()
    configure_logging(settings)
    redis = ctx["redis"]

    ctx["settings"] = settings
    ctx["http"] = httpx.AsyncClient(timeout=10.0)
    ctx["aggregator"] = ConversationAggregator(redis, ttl_seconds=settings.aggregation_ttl_seconds)
    ctx["rate_limiter"] = ContactRateLimiter(
        redis,
        max_turns=settings.contact_rate_limit_max_turns,
        window_seconds=settings.contact_rate_limit_window_seconds,
    )
    ctx["outbound"] = ArqOutboundQueue(redis=redis)
    _install_agent(ctx, settings)
    log.info("worker_iniciado", environment=settings.environment)


def _install_agent(ctx: WorkerContext, settings: Settings) -> None:
    """Instala o motor do agente como `turn_handler` da fila de entrada (S06).

    O import e local de proposito: `app.agent.runner` depende deste modulo para ler o
    `ctx`, e importa-lo no topo fecharia um ciclo. Sem chave de API o worker sobe do
    mesmo jeito, com o handler que so registra a rajada — e o que permite rodar
    agregacao, fila e webhook em desenvolvimento sem gastar token.

    Tools de leitura entram na S08 (`search_knowledge`, `list_services`); handoff na
    S10; tools de escrita na S15.
    """
    if not settings.anthropic_api_key:
        log.warning("agente_sem_chave_de_api", detalhe="turno nao chamara o modelo")
        return

    from app.agent.audit import PostgresAuditSink
    from app.agent.engine import AgentEngine
    from app.agent.llm import AnthropicProvider
    from app.agent.runner import install
    from app.agent.tools.registry import build_registry

    install(
        ctx,
        AgentEngine(
            provider=AnthropicProvider.from_settings(settings),
            tools=build_registry(embeddings=_embedding_provider(settings), settings=settings),
            audit=PostgresAuditSink(settings),
            max_iterations=settings.max_tool_iterations,
        ),
    )


def _embedding_provider(settings: Settings) -> EmbeddingProvider | None:
    """`HashingEmbeddingProvider` nao tem qualidade semantica real (D-26): so serve fora
    de producao. Sem fornecedor real ainda, `search_knowledge` fica de fora do registro
    em vez de responder com relevancia falsa (D-27)."""
    if settings.environment in ("local", "test"):
        from app.knowledge.embeddings import HashingEmbeddingProvider

        return HashingEmbeddingProvider()
    log.warning("embedding_provider_indisponivel", detalhe="search_knowledge desativada")
    return None


async def on_shutdown(ctx: WorkerContext) -> None:
    client: httpx.AsyncClient | None = ctx.get("http")
    if client is not None:
        await client.aclose()
    await dispose_engine()
    log.info("worker_encerrado")


def ctx_settings(ctx: WorkerContext) -> Settings:
    settings: Settings = ctx["settings"]
    return settings


def ctx_aggregator(ctx: WorkerContext) -> ConversationAggregator:
    aggregator: ConversationAggregator = ctx["aggregator"]
    return aggregator


def ctx_rate_limiter(ctx: WorkerContext) -> ContactRateLimiter:
    limiter: ContactRateLimiter = ctx["rate_limiter"]
    return limiter


def ctx_outbound(ctx: WorkerContext) -> ArqOutboundQueue:
    """Produtor da fila de saida. Quem publica resposta e o motor do agente (S06)."""
    outbound: ArqOutboundQueue = ctx["outbound"]
    return outbound


def channel_for(ctx: WorkerContext, config: TenantConfig) -> OutboundChannel | None:
    """Canal de saida do tenant, ou `None` quando nao da para enviar.

    Sem token configurado nao ha envio possivel — em desenvolvimento isso e o normal, e
    o worker precisa continuar rodando o resto do fluxo em vez de estourar. O canal web
    entra na S21; ate la, so WhatsApp.
    """
    # Ponto de injecao: os testes trocam o canal inteiro por um duble sem tocar em HTTP.
    factory = ctx.get("channel_factory")
    if factory is not None:
        channel: OutboundChannel | None = factory(ctx, config)
        return channel

    settings = ctx_settings(ctx)
    whatsapp = config.channels.whatsapp
    if whatsapp is None or not whatsapp.enabled:
        return None
    if not settings.whatsapp_access_token:
        return None
    return WhatsAppSender(
        phone_number_id=whatsapp.phone_number_id,
        access_token=settings.whatsapp_access_token,
        api_version=settings.whatsapp_api_version,
        base_url=settings.whatsapp_api_base_url,
        client=ctx["http"],
    )
