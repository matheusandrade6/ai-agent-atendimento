"""Fila de saida: backoff, 429 e idempotencia (secao 14.1.3, Sessao S05).

O canal e um duble — o que esta sob teste e a **politica**: quando reagendar, quando
desistir, quando nem tentar, e o que fica registrado em `messages` em cada caso. A
traducao de HTTP para excecao tem teste proprio em `tests/unit/test_whatsapp_sender.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from arq import Retry
from redis.asyncio import Redis

from app.channels.base import (
    ChannelPermanentError,
    ChannelRateLimited,
    ChannelTransientError,
    SentMessage,
)
from app.core.config import Settings
from app.core.db import dispose_engine
from app.core.time import now_utc
from app.workers.base import WorkerContext
from app.workers.outbound import SENT_KEY, backoff_delay, send_outbound
from app.workers.queue import OutboundMessage

pytestmark = pytest.mark.integration

WA_ID = "5511999998888"


class FakeChannel:
    """Canal que devolve o que o teste mandar: um recibo ou uma excecao."""

    def __init__(self, result: SentMessage | Exception) -> None:
        self._result = result
        self.enviados: list[tuple[str, str]] = []

    async def send_text(self, *, to: str, text: str) -> SentMessage:
        self.enviados.append((to, text))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def mark_read(self, *, provider_msg_id: str, typing: bool = False) -> None:
        return None

    async def aclose(self) -> None:
        return None


@pytest.fixture
def conversation(
    sync_engine: sa.Engine, tenant_whatsapp: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    """Conversa com a janela de servico aberta (24h a partir de agora)."""
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        conversation_id = conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(tenant_id, contact_id, channel, channel_thread, service_window_expires_at) "
                "VALUES (:t, :c, 'whatsapp', :thread, :expires) RETURNING id"
            ),
            {
                "t": tenant_whatsapp["tenant_id"],
                "c": tenant_whatsapp["contact_id"],
                "thread": WA_ID,
                "expires": now_utc() + timedelta(hours=24),
            },
        ).scalar_one()
    yield {"conversation_id": conversation_id, **tenant_whatsapp}


def _close_window(sync_engine: sa.Engine, conversation_id: uuid.UUID) -> None:
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        conn.execute(
            sa.text("UPDATE conversations SET service_window_expires_at = :e WHERE id = :id"),
            {"e": now_utc() - timedelta(minutes=1), "id": conversation_id},
        )


def _outbound_rows(sync_engine: sa.Engine, conversation_id: uuid.UUID) -> list[Any]:
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        return list(
            conn.execute(
                sa.text(
                    "SELECT status, content, provider_msg_id FROM messages "
                    "WHERE conversation_id = :id AND direction = 'outbound'"
                ),
                {"id": conversation_id},
            ).all()
        )


@pytest.fixture
async def ctx(settings: Settings, redis_client: Redis) -> AsyncIterator[WorkerContext]:
    context: WorkerContext = {"settings": settings, "redis": redis_client}
    yield context
    await dispose_engine()


def _with_channel(ctx: WorkerContext, channel: FakeChannel) -> FakeChannel:
    ctx["channel_factory"] = lambda _ctx, _config: channel
    return channel


def _message(conversation: dict[str, Any], text: str = "Posso agendar sexta as 10h?") -> Any:
    return OutboundMessage(
        tenant_id=conversation["tenant_id"],
        conversation_id=conversation["conversation_id"],
        text=text,
        idempotency_key=f"turno-{uuid.uuid4().hex}",
    ).as_payload()


async def test_envio_persiste_a_mensagem_e_nao_repete_na_reentrega(
    ctx: WorkerContext, conversation: dict[str, Any], sync_engine: sa.Engine, redis_client: Redis
) -> None:
    canal = _with_channel(ctx, FakeChannel(SentMessage(provider_msg_id="wamid.ENVIADA")))
    payload = _message(conversation)

    assert await send_outbound(ctx, payload) == "enviada"
    # Mesma chave de idempotencia chegando de novo: nao pode virar segunda mensagem.
    assert await send_outbound(ctx, payload) == "duplicada"

    assert canal.enviados == [(WA_ID, "Posso agendar sexta as 10h?")]
    rows = _outbound_rows(sync_engine, conversation["conversation_id"])
    assert len(rows) == 1
    assert rows[0].status == "sent"
    assert rows[0].provider_msg_id == "wamid.ENVIADA"

    sent_key = SENT_KEY.format(
        tenant_id=conversation["tenant_id"], idempotency_key=payload["idempotency_key"]
    )
    assert await redis_client.get(sent_key) == b"wamid.ENVIADA"


async def test_429_reagenda_honrando_retry_after(
    ctx: WorkerContext, conversation: dict[str, Any], sync_engine: sa.Engine
) -> None:
    _with_channel(ctx, FakeChannel(ChannelRateLimited("429", retry_after=30.0)))

    with pytest.raises(Retry) as exc:
        await send_outbound(ctx, _message(conversation))

    assert exc.value.defer_score == 30_000
    assert _outbound_rows(sync_engine, conversation["conversation_id"]) == []


async def test_429_sem_retry_after_usa_backoff_exponencial(
    ctx: WorkerContext, conversation: dict[str, Any], settings: Settings
) -> None:
    _with_channel(ctx, FakeChannel(ChannelRateLimited("429")))
    ctx["job_try"] = 3

    with pytest.raises(Retry) as exc:
        await send_outbound(ctx, _message(conversation))

    base = settings.outbound_backoff_base_seconds
    esperado = min(settings.outbound_backoff_max_seconds, base * 2**2)
    assert exc.value.defer_score is not None
    assert esperado / 2 * 1000 <= exc.value.defer_score <= esperado * 1000


async def test_falha_transitoria_no_ultimo_try_desiste_e_marca_falha(
    ctx: WorkerContext, conversation: dict[str, Any], sync_engine: sa.Engine, settings: Settings
) -> None:
    """Esgotadas as tentativas, a mensagem vira linha `failed` — nao silencio."""
    _with_channel(ctx, FakeChannel(ChannelTransientError("503")))
    ctx["job_try"] = settings.outbound_max_tries

    assert await send_outbound(ctx, _message(conversation)) == "desistiu"

    rows = _outbound_rows(sync_engine, conversation["conversation_id"])
    assert [row.status for row in rows] == ["failed"]


async def test_falha_permanente_nao_retenta(
    ctx: WorkerContext, conversation: dict[str, Any], sync_engine: sa.Engine
) -> None:
    _with_channel(ctx, FakeChannel(ChannelPermanentError("400 numero invalido")))

    assert await send_outbound(ctx, _message(conversation)) == "falha_permanente"

    rows = _outbound_rows(sync_engine, conversation["conversation_id"])
    assert [row.status for row in rows] == ["failed"]


async def test_fora_da_janela_de_servico_nao_manda_texto_livre(
    ctx: WorkerContext, conversation: dict[str, Any], sync_engine: sa.Engine
) -> None:
    """Passadas as 24h, so template utility (14.1.4). O canal nem chega a ser chamado."""
    canal = _with_channel(ctx, FakeChannel(SentMessage(provider_msg_id="wamid.NAO_DEVIA")))
    _close_window(sync_engine, conversation["conversation_id"])

    assert await send_outbound(ctx, _message(conversation)) == "janela_fechada"

    assert canal.enviados == []
    rows = _outbound_rows(sync_engine, conversation["conversation_id"])
    assert [row.status for row in rows] == ["blocked"]


def test_backoff_cresce_e_respeita_o_teto() -> None:
    """Sem rede: a curva do backoff e uma funcao pura e tem teste de unidade proprio."""
    import random

    rng = random.Random(42)
    atrasos = [backoff_delay(t, base=2.0, cap=60.0, rng=rng) for t in range(1, 8)]

    assert atrasos[0] < atrasos[1] < atrasos[2], "a espera precisa crescer"
    assert all(atraso <= 60.0 for atraso in atrasos), "o teto vale"
    # Equal jitter: nunca abaixo da metade do termo exponencial.
    assert atrasos[0] >= 1.0
