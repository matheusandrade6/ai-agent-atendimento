"""Worker de saida: entrega com backoff exponencial e respeito a 429 (secao 14.1.3).

Quem decide retentar e este modulo, nao o canal. O canal traduz HTTP para tres
excecoes (`app.channels.base`) e a politica vive aqui:

| Falha | O que fazemos |
|---|---|
| `429` | reagenda honrando `Retry-After`; sem cabecalho, backoff exponencial |
| `5xx`, timeout | reagenda com backoff exponencial |
| `4xx` (fora 429) | marca `failed` e para — retentar so gasta cota |

O backoff usa *equal jitter*: metade fixa, metade sorteada. Sem o sorteio, mil jobs que
tomaram 429 juntos voltariam juntos e tomariam 429 de novo.

**Idempotencia (invariante 5).** A reentrega de um job nao pode virar duas mensagens
para a pessoa. Uma chave em Redis registra o envio ja concluido; o job que a encontra
nao envia de novo. A janela que sobra e a do timeout apos o envio ter chegado — nesse
caso a chave nao foi gravada e o texto pode sair duas vezes. A Cloud API nao oferece
chave de idempotencia para fechar isso; o preco de errar para o outro lado (silencio
depois de uma pergunta) e maior.
"""

from __future__ import annotations

import random
import uuid
from typing import Any

import sqlalchemy as sa
from arq import Retry
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import (
    ChannelPermanentError,
    ChannelRateLimited,
    ChannelTransientError,
    SentMessage,
)
from app.core.config import Settings
from app.core.db import tenant_session
from app.core.telemetry import get_logger, log_context
from app.core.time import now_utc
from app.domain.tenant_registry import load_config_by_tenant_id
from app.workers.base import WorkerContext, channel_for, ctx_settings
from app.workers.queue import OutboundMessage

log = get_logger(__name__)

#: Registro de envio concluido, por chave de idempotencia do turno.
SENT_KEY = "outb:sent:{tenant_id}:{idempotency_key}"

_rng = random.Random()


def backoff_delay(
    job_try: int, *, base: float, cap: float, rng: random.Random | None = None
) -> float:
    """Espera da tentativa `job_try` (1 = primeira), com *equal jitter*."""
    exponential: float = min(cap, base * 2 ** max(0, job_try - 1))
    half = exponential / 2
    delay: float = half + (rng or _rng).uniform(0, half)
    return round(delay, 3)


async def send_outbound(ctx: WorkerContext, payload: dict[str, Any]) -> str:
    message = OutboundMessage.model_validate(payload)
    settings = ctx_settings(ctx)
    job_try = int(ctx.get("job_try", 1))
    sent_key = SENT_KEY.format(tenant_id=message.tenant_id, idempotency_key=message.idempotency_key)

    with log_context(
        tenant_id=str(message.tenant_id), conversation_id=str(message.conversation_id)
    ):
        if await ctx["redis"].get(sent_key):
            log.info("outbound_ja_enviada", idempotency_key=message.idempotency_key)
            return "duplicada"

        async with tenant_session(message.tenant_id, settings) as session:
            destination = await _destination(session, message.conversation_id)
        if destination is None:
            log.error("outbound_conversa_inexistente")
            return "sem_conversa"

        channel_name, thread, window_open = destination
        if channel_name != "whatsapp":
            # O canal web (S21) tem transporte proprio, sem fila de saida.
            log.error("outbound_canal_nao_suportado", canal=channel_name)
            return "canal_nao_suportado"
        if not window_open:
            # Fora da janela de 24h, texto livre e recusado pela Meta: o envio proativo
            # exige template utility aprovado (14.1.4), que entra na S19.
            log.error("outbound_janela_de_servico_fechada")
            await _persist(settings=settings, message=message, sent=None, status="blocked")
            return "janela_fechada"

        config = await load_config_by_tenant_id(message.tenant_id, settings)
        if config is None:
            log.error("outbound_tenant_sem_config")
            return "sem_config"
        channel = channel_for(ctx, config)
        if channel is None:
            log.error("outbound_sem_canal_configurado")
            return "sem_canal"

        try:
            sent = await channel.send_text(to=thread, text=message.text)
        except ChannelRateLimited as exc:
            delay = exc.retry_after or backoff_delay(
                job_try,
                base=settings.outbound_backoff_base_seconds,
                cap=settings.outbound_backoff_max_seconds,
            )
            _raise_or_give_up(exc, job_try, settings.outbound_max_tries, delay, motivo="429")
            await _persist(settings=settings, message=message, sent=None, status="failed")
            return "desistiu"
        except ChannelTransientError as exc:
            delay = backoff_delay(
                job_try,
                base=settings.outbound_backoff_base_seconds,
                cap=settings.outbound_backoff_max_seconds,
            )
            _raise_or_give_up(
                exc, job_try, settings.outbound_max_tries, delay, motivo="transitorio"
            )
            await _persist(settings=settings, message=message, sent=None, status="failed")
            return "desistiu"
        except ChannelPermanentError:
            log.exception("outbound_falha_permanente")
            await _persist(settings=settings, message=message, sent=None, status="failed")
            return "falha_permanente"

        await ctx["redis"].set(
            sent_key, sent.provider_msg_id, ex=settings.outbound_idempotency_ttl_seconds
        )
        await _persist(settings=settings, message=message, sent=sent, status="sent")
        log.info("outbound_enviada", provider_msg_id=sent.provider_msg_id, tentativa=job_try)
        return "enviada"


def _raise_or_give_up(
    exc: Exception, job_try: int, max_tries: int, delay: float, *, motivo: str
) -> None:
    """Reagenda a tentativa, ou volta para quem chamou quando o teto foi atingido."""
    if job_try < max_tries:
        log.warning("outbound_reagendada", motivo=motivo, tentativa=job_try, defer_seconds=delay)
        raise Retry(defer=delay) from exc
    log.error("outbound_desistiu", motivo=motivo, tentativas=job_try)


async def _destination(
    session: AsyncSession, conversation_id: uuid.UUID
) -> tuple[str, str, bool] | None:
    """`(canal, thread, janela_aberta)` da conversa, ou `None` se ela nao existe."""
    row = (
        await session.execute(
            sa.text(
                "SELECT channel, channel_thread, service_window_expires_at "
                "FROM conversations WHERE id = :id"
            ),
            {"id": conversation_id},
        )
    ).first()
    if row is None:
        return None
    expires_at = row.service_window_expires_at
    return (
        str(row.channel),
        str(row.channel_thread),
        expires_at is not None and expires_at > now_utc(),
    )


async def _persist(
    *, settings: Settings, message: OutboundMessage, sent: SentMessage | None, status: str
) -> None:
    """Registra a mensagem de saida.

    Ate uma tentativa recusada vira linha: a caixa de conversas do painel (15.2) precisa
    mostrar que a resposta nao saiu, senao o atendente ve um silencio inexplicado.
    """
    # `billing_category = free`: so sai texto livre dentro da janela de 24h, e dentro
    # dela a Meta nao cobra (14.1.4). Template utility (pago) entra na S19.
    async with tenant_session(message.tenant_id, settings) as session:
        await session.execute(
            sa.text(
                "INSERT INTO messages "
                "(tenant_id, conversation_id, direction, author, content_type, content, "
                " provider_msg_id, status, billing_category) "
                "VALUES (:tenant_id, :conversation_id, 'outbound', 'agent', 'text', :content, "
                " :provider_msg_id, :status, 'free') "
                "ON CONFLICT (tenant_id, provider_msg_id) DO NOTHING"
            ),
            {
                "tenant_id": message.tenant_id,
                "conversation_id": message.conversation_id,
                "content": message.text,
                "provider_msg_id": sent.provider_msg_id if sent else None,
                "status": status,
            },
        )
