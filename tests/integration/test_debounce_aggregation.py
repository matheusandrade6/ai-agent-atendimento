"""Agregacao por debounce: um turno por rajada (secao 14.1.2, Sessao S05).

Estes testes rodam contra Redis real de proposito. A garantia inteira mora na
atomicidade dos dois scripts Lua — um fake em memoria provaria o fluxo feliz e
esconderia exatamente a corrida que a sessao existe para fechar.

O relogio e simulado: `process_inbound` agenda o disparo com `_defer_by`, e o teste
executa os disparos agendados na ordem em que foram criados. E o mesmo que o arq faria
ao acordar cada job, sem esperar seis segundos por teste.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from redis.asyncio import Redis

from app.api.webhooks.whatsapp import _process_payload
from app.core.config import Settings
from app.core.db import dispose_engine
from app.workers.aggregator import AggregatedTurn, ConversationAggregator
from app.workers.base import WorkerContext
from app.workers.inbound import flush_conversation, process_inbound
from app.workers.queue import FLUSH_JOB_NAME, InboundMessage
from app.workers.ratelimit import ContactRateLimiter

pytestmark = pytest.mark.integration


class RecordingRedis:
    """Pool de Redis real, com os `enqueue_job` capturados em vez de enfileirados.

    Capturar o agendamento e o que deixa o teste controlar o relogio: cada job gravado
    aqui e um disparo que o arq acordaria mais tarde.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client
        self.jobs: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> None:
        self.jobs.append((function, args, kwargs))

    @property
    def flushes(self) -> list[tuple[Any, ...]]:
        return [args for name, args, _ in self.jobs if name == FLUSH_JOB_NAME]


class TurnRecorder:
    """Handler de turno que so registra. A S06 poe o motor do agente no lugar dele."""

    def __init__(self) -> None:
        self.turns: list[AggregatedTurn] = []

    async def __call__(self, ctx: WorkerContext, turn: AggregatedTurn) -> None:
        self.turns.append(turn)


@pytest.fixture
async def ctx(
    settings: Settings, redis_client: Redis, tenant_whatsapp: dict[str, Any]
) -> AsyncIterator[WorkerContext]:
    recorder = TurnRecorder()
    context: WorkerContext = {
        "settings": settings,
        "redis": RecordingRedis(redis_client),
        # TTL folgado de proposito: o relogio da VM do Docker Desktop no Windows anda
        # atrasado e corrige de uma vez (saltos de ~1 min observados). Com TTL curto, o
        # salto expira o buffer no meio do teste e a falha parece corrida quando e o
        # relogio. Em producao o default de `aggregation_ttl_seconds` (900s) vale.
        "aggregator": ConversationAggregator(redis_client, ttl_seconds=3600),
        "rate_limiter": ContactRateLimiter(redis_client, max_turns=50, window_seconds=60),
        "turn_handler": recorder,
        "recorder": recorder,
        # Sem canal: marcar lida e cortesia, e nenhum teste daqui depende de HTTP.
        "channel_factory": lambda _ctx, _config: None,
    }
    yield context
    # O engine assincrono e global e cada teste roda no seu proprio event loop.
    await dispose_engine()


def _message(tenant: dict[str, Any], conversation_id: uuid.UUID, text: str) -> InboundMessage:
    return InboundMessage(
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        contact_id=tenant["contact_id"],
        channel="whatsapp",
        message_id=uuid.uuid4(),
        provider_msg_id=f"wamid.{uuid.uuid4().hex}",
        content_type="text",
        text=text,
    )


async def _run_scheduled_flushes(ctx: WorkerContext, *, since: int = 0) -> list[str]:
    """Executa os disparos ja agendados, na ordem em que o arq os acordaria."""
    redis: RecordingRedis = ctx["redis"]
    return [await flush_conversation(ctx, *args) for args in redis.flushes[since:]]


def _delivered_ids(recorder: TurnRecorder) -> list[uuid.UUID]:
    return [part.message_id for turn in recorder.turns for part in turn.parts]


# ------------------------------- aceite da sessao -------------------------------


async def test_tres_mensagens_na_mesma_rajada_produzem_um_unico_turno(
    ctx: WorkerContext, tenant_whatsapp: dict[str, Any]
) -> None:
    """Tres baloes dentro da janela de debounce viram um turno com o texto na ordem."""
    conversation_id = uuid.uuid4()
    textos = ["Oi", "queria marcar um horario", "pra sexta de manha"]

    for texto in textos:
        mensagem = _message(tenant_whatsapp, conversation_id, texto)
        assert await process_inbound(ctx, mensagem.as_payload()) == "bufferizada"
        # Intervalo real entre os baloes: as tres chegam em menos de 2s, bem dentro
        # dos 6s de debounce do tenant.
        await asyncio.sleep(0.05)

    redis: RecordingRedis = ctx["redis"]
    assert len(redis.flushes) == 3, "cada mensagem reinicia o timer agendando o seu disparo"
    assert [args[2] for args in redis.flushes] == [1, 2, 3]
    assert all(job[2]["_defer_by"] == timedelta(seconds=6) for job in redis.jobs)

    results = await _run_scheduled_flushes(ctx)

    assert results == ["timer_reiniciado", "timer_reiniciado", "turno_disparado"]
    recorder: TurnRecorder = ctx["recorder"]
    assert len(recorder.turns) == 1
    assert recorder.turns[0].text == "Oi\nqueria marcar um horario\npra sexta de manha"
    assert len(recorder.turns[0].parts) == 3


async def test_disparo_repetido_nao_gera_segundo_turno(
    ctx: WorkerContext, tenant_whatsapp: dict[str, Any]
) -> None:
    """Reentrega do proprio job de disparo: o buffer ja foi consumido, nada acontece."""
    conversation_id = uuid.uuid4()
    await process_inbound(ctx, _message(tenant_whatsapp, conversation_id, "Oi").as_payload())

    redis: RecordingRedis = ctx["redis"]
    primeiro = await flush_conversation(ctx, *redis.flushes[0])
    segundo = await flush_conversation(ctx, *redis.flushes[0])

    assert primeiro == "turno_disparado"
    assert segundo == "vazio"
    assert len(ctx["recorder"].turns) == 1


async def test_mensagem_que_chega_junto_do_disparo_nao_se_perde(
    ctx: WorkerContext, tenant_whatsapp: dict[str, Any]
) -> None:
    """A corrida: a terceira mensagem entra no mesmo instante em que a rajada fecha.

    Os dois desfechos sao corretos e o teste aceita os dois — o que ele nao aceita e
    mensagem entregue duas vezes ou nenhuma:

    - a mensagem entra antes do disparo, o contador avanca, aquele disparo recusa e o
      disparo dela leva a rajada inteira (um turno com tres partes);
    - a mensagem entra depois, a rajada fecha com duas partes e ela abre a proxima
      (dois turnos, 2 + 1).

    Roda em varias rodadas porque o interleaving depende do agendador do event loop.
    """
    recorder: TurnRecorder = ctx["recorder"]

    for _ in range(15):
        conversation_id = uuid.uuid4()
        redis: RecordingRedis = ctx["redis"]
        redis.jobs.clear()
        recorder.turns.clear()

        textos = ("Oi", "boa tarde", "ainda ai?")
        enviadas = [_message(tenant_whatsapp, conversation_id, t) for t in textos]
        for message in enviadas[:2]:
            await process_inbound(ctx, message.as_payload())

        # Disparo da rajada de duas e chegada da terceira, ao mesmo tempo.
        await asyncio.gather(
            flush_conversation(ctx, *redis.flushes[-1]),
            process_inbound(ctx, enviadas[2].as_payload()),
        )
        await _run_scheduled_flushes(ctx)

        entregues = _delivered_ids(recorder)
        assert sorted(map(str, entregues)) == sorted(str(m.message_id) for m in enviadas)
        assert len(entregues) == len(set(entregues)), "mensagem entregue em dois turnos"
        assert all(turn.parts for turn in recorder.turns), "turno vazio disparado"
        # O texto de cada turno preserva a ordem de chegada.
        for turn in recorder.turns:
            posicoes = [enviadas.index(_por_id(enviadas, part.message_id)) for part in turn.parts]
            assert posicoes == sorted(posicoes)


def _por_id(mensagens: list[InboundMessage], message_id: uuid.UUID) -> InboundMessage:
    return next(m for m in mensagens if m.message_id == message_id)


async def test_reentrega_do_mesmo_job_nao_duplica_a_parte(
    ctx: WorkerContext, tenant_whatsapp: dict[str, Any]
) -> None:
    """Job reentregue pelo arq: a parte entra uma vez so, e o disparo e reagendado."""
    conversation_id = uuid.uuid4()
    payload = _message(tenant_whatsapp, conversation_id, "Oi").as_payload()

    assert await process_inbound(ctx, payload) == "bufferizada"
    assert await process_inbound(ctx, payload) == "duplicada"

    redis: RecordingRedis = ctx["redis"]
    assert len(redis.flushes) == 2, "a reentrega reagenda o disparo para nao deixar parte presa"
    assert [args[2] for args in redis.flushes] == [1, 1]

    results = await _run_scheduled_flushes(ctx)

    assert results.count("turno_disparado") == 1
    recorder: TurnRecorder = ctx["recorder"]
    assert len(recorder.turns) == 1
    assert recorder.turns[0].text == "Oi"


async def test_reentrega_do_webhook_nao_duplica_turno(
    ctx: WorkerContext, tenant_whatsapp: dict[str, Any], settings: Settings
) -> None:
    """Ponta a ponta: a Meta reentrega o mesmo payload e sai um turno so.

    A primeira barreira e a constraint de dedup da S04 — a segunda entrega nao chega
    nem a enfileirar. A segunda barreira e o buffer, exercitada no teste acima.
    """
    collected: list[InboundMessage] = []

    class CollectingQueue:
        async def enqueue_inbound(self, message: InboundMessage) -> None:
            collected.append(message)

        async def close(self) -> None:
            return None

    body = json.dumps(_webhook_payload(tenant_whatsapp["phone_number_id"])).encode("utf-8")
    queue = CollectingQueue()
    await _process_payload(body, queue, settings)
    await _process_payload(body, queue, settings)

    assert len(collected) == 1, "a reentrega da Meta nao enfileira de novo"

    await process_inbound(ctx, collected[0].as_payload())
    results = await _run_scheduled_flushes(ctx)

    assert results == ["turno_disparado"]
    recorder: TurnRecorder = ctx["recorder"]
    assert len(recorder.turns) == 1
    assert recorder.turns[0].text == "Oi, quero marcar um horario"


async def test_rate_limit_por_contato_descarta_o_turno(
    ctx: WorkerContext, tenant_whatsapp: dict[str, Any], redis_client: Redis
) -> None:
    """Anti-flood (11.5): o segundo turno do mesmo contato na janela nao passa."""
    ctx["rate_limiter"] = ContactRateLimiter(redis_client, max_turns=1, window_seconds=60)

    resultados = []
    for texto in ("Oi", "e ai"):
        conversation_id = uuid.uuid4()
        await process_inbound(ctx, _message(tenant_whatsapp, conversation_id, texto).as_payload())
        resultados.append(await flush_conversation(ctx, *ctx["redis"].flushes[-1]))

    assert resultados == ["turno_disparado", "rate_limited"]
    assert len(ctx["recorder"].turns) == 1


def _webhook_payload(phone_number_id: str) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": phone_number_id},
                            "contacts": [{"profile": {"name": "Carla"}, "wa_id": "5511999998888"}],
                            "messages": [
                                {
                                    "from": "5511999998888",
                                    "id": f"wamid.{uuid.uuid4().hex}",
                                    "timestamp": "1699999999",
                                    "type": "text",
                                    "text": {"body": "Oi, quero marcar um horario"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
