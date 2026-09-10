"""Worker de entrada: agregacao por debounce e disparo do turno (secoes 14.1.2 e 11.1).

Dois jobs, de proposito:

- `process_inbound` roda assim que a mensagem e persistida. Ele so joga a mensagem no
  buffer e agenda o disparo para daqui a `debounce_seconds`. E rapido e nao decide nada.
- `flush_conversation` roda depois, diferido. E ele que fecha a rajada — se ninguem
  chegou no meio tempo — e chama o turno do agente.

Separar os dois e o que faz "cada mensagem reinicia o timer" custar um agendamento em
vez de um cancelamento: nao ha timer para cancelar, ha varios disparos agendados dos
quais **um so** encontra a sequencia que esperava. A prova de que isso nao duplica nem
perde mensagem esta no docstring de `app.workers.aggregator`.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Protocol

from app.core.telemetry import get_logger, log_context
from app.domain.tenant_config import TenantConfig
from app.domain.tenant_registry import load_config_by_tenant_id
from app.workers.aggregator import AggregatedTurn
from app.workers.base import (
    WorkerContext,
    channel_for,
    ctx_aggregator,
    ctx_rate_limiter,
    ctx_settings,
)
from app.workers.queue import FLUSH_JOB_NAME, InboundMessage

log = get_logger(__name__)


class TurnHandler(Protocol):
    """O que fazer com uma rajada fechada.

    A S06 instala o motor do agente aqui (`ctx["turn_handler"]`). Enquanto ele nao
    existe, o padrao registra o turno e para — o que ja e suficiente para provar a
    agregacao ponta a ponta sem depender de LLM.
    """

    async def __call__(self, ctx: WorkerContext, turn: AggregatedTurn) -> None: ...


async def process_inbound(ctx: WorkerContext, payload: dict[str, Any]) -> str:
    """Bufferiza a mensagem e (re)agenda o disparo da rajada."""
    message = InboundMessage.model_validate(payload)
    with log_context(
        tenant_id=str(message.tenant_id), conversation_id=str(message.conversation_id)
    ):
        result = await ctx_aggregator(ctx).append(message)
        if result.seq == 0:
            # Reentrega de uma mensagem cujo buffer ja expirou por TTL. Reprocessar
            # abriria uma rajada com conteudo velho; o turno daquela mensagem ja foi.
            log.info("inbound_reentrega_expirada", message_id=str(message.message_id))
            return "expirada"

        debounce = await _debounce_seconds(ctx, message)
        # Mesmo numa reentrega (`buffered=False`) o disparo e reagendado: se o worker
        # morreu entre bufferizar e agendar, esta e a unica chance de a ultima mensagem
        # da rajada sair do buffer sem esperar a proxima mensagem da pessoa.
        await ctx["redis"].enqueue_job(
            FLUSH_JOB_NAME,
            str(message.tenant_id),
            str(message.conversation_id),
            result.seq,
            _defer_by=timedelta(seconds=debounce),
        )
        log.info(
            "inbound_bufferizada",
            message_id=str(message.message_id),
            seq=result.seq,
            debounce_seconds=debounce,
            duplicada=not result.buffered,
        )
        return "bufferizada" if result.buffered else "duplicada"


async def flush_conversation(
    ctx: WorkerContext, tenant_id: str, conversation_id: str, expected_seq: int
) -> str:
    """Fecha a rajada e dispara **um** turno, se a sequencia ainda for a esperada."""
    tenant_uuid = uuid.UUID(tenant_id)
    conversation_uuid = uuid.UUID(conversation_id)

    with log_context(tenant_id=tenant_id, conversation_id=conversation_id):
        parts = await ctx_aggregator(ctx).drain(conversation_uuid, expected_seq)
        if parts is None:
            # Chegou mensagem depois desta: o disparo dela e mais novo e leva a rajada.
            log.debug("flush_timer_reiniciado", seq=expected_seq)
            return "timer_reiniciado"
        if not parts:
            # Um disparo irmao ja levou as partes. Nao ha turno a disparar.
            log.debug("flush_buffer_vazio", seq=expected_seq)
            return "vazio"

        turn = AggregatedTurn(
            tenant_id=tenant_uuid,
            conversation_id=conversation_uuid,
            contact_id=parts[0].contact_id,
            parts=parts,
        )

        allowed = await ctx_rate_limiter(ctx).allow(
            tenant_id=tenant_uuid, contact_id=turn.contact_id
        )
        if not allowed:
            # Anti-flood (11.5): a rajada foi consumida e nao vira turno. Silencio e a
            # resposta certa — avisar do limite so alimenta o flood.
            log.warning("inbound_rate_limit_excedido", contact_id=str(turn.contact_id))
            return "rate_limited"

        config = await load_config_by_tenant_id(tenant_uuid, ctx_settings(ctx))
        if config is not None:
            await _mark_read(ctx, turn, config)

        handler: TurnHandler = ctx.get("turn_handler") or _log_turn
        await handler(ctx, turn)
        log.info("turno_disparado", partes=len(turn.parts), seq=expected_seq)
        return "turno_disparado"


async def _debounce_seconds(ctx: WorkerContext, message: InboundMessage) -> int:
    """Debounce do canal, com o default global quando o tenant nao diz nada."""
    settings = ctx_settings(ctx)
    config = await load_config_by_tenant_id(message.tenant_id, settings)
    if config is None:
        return settings.default_debounce_seconds
    if message.channel == "whatsapp" and config.channels.whatsapp is not None:
        return config.channels.whatsapp.debounce_seconds
    if message.channel == "web":
        return config.channels.web.debounce_seconds
    return settings.default_debounce_seconds


async def _mark_read(ctx: WorkerContext, turn: AggregatedTurn, config: TenantConfig) -> None:
    """Marca lida e liga o "digitando" enquanto o turno roda (14.1.3).

    Cortesia, nao contrato: falhar aqui nunca pode impedir a resposta.
    """
    provider_msg_id = turn.last_provider_msg_id
    if provider_msg_id is None:
        return
    channel = channel_for(ctx, config)
    if channel is None:
        return
    try:
        await channel.mark_read(provider_msg_id=provider_msg_id, typing=True)
    except Exception:
        log.info("mark_read_falhou", provider_msg_id=provider_msg_id)


async def _log_turn(ctx: WorkerContext, turn: AggregatedTurn) -> None:
    """Handler padrao ate a S06: registra a rajada agregada e nao chama LLM."""
    log.info("turno_agregado_sem_handler", texto=turn.text, partes=len(turn.parts))
