"""Turno ponta a ponta com banco real: carga de estado, persistencia e enfileiramento.

O motor tem teste proprio sem banco (`tests/unit/test_agent_engine.py`). Aqui esta sob
teste o que so o Postgres pode provar: que tokens e custo chegam na coluna, que o
`collected` e o `stage` sobrevivem ao turno, e que o resumo rolante grava o contador que
faz o proximo gatilho cair na hora certa (secao 11.7).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa

from app.agent.engine import AgentEngine
from app.agent.runner import AgentTurnHandler, load_state
from app.core.config import Settings
from app.core.db import dispose_engine
from app.workers.aggregator import AggregatedTurn
from app.workers.base import WorkerContext
from app.workers.queue import InboundMessage, OutboundMessage
from tests.fakes import ScriptedProvider, text_response

pytestmark = pytest.mark.integration

WA_ID = "5511977776666"


class FakeOutboundQueue:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def enqueue_outbound(self, message: OutboundMessage) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        return None


@pytest.fixture
def conversation(
    sync_engine: sa.Engine, tenant_whatsapp: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        conversation_id = conn.execute(
            sa.text(
                "INSERT INTO conversations (tenant_id, contact_id, channel, channel_thread)"
                " VALUES (:t, :c, 'whatsapp', :thread) RETURNING id"
            ),
            {
                "t": tenant_whatsapp["tenant_id"],
                "c": tenant_whatsapp["contact_id"],
                "thread": WA_ID,
            },
        ).scalar_one()
    yield {"conversation_id": conversation_id, **tenant_whatsapp}


def _insert_messages(
    sync_engine: sa.Engine, conversation: dict[str, Any], texts: list[tuple[str, str]]
) -> None:
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        for direction, content in texts:
            conn.execute(
                sa.text(
                    "INSERT INTO messages (tenant_id, conversation_id, direction, author, content)"
                    " VALUES (:t, :c, :d, :a, :content)"
                ),
                {
                    "t": conversation["tenant_id"],
                    "c": conversation["conversation_id"],
                    "d": direction,
                    "a": "contact" if direction == "inbound" else "agent",
                    "content": content,
                },
            )


def _rows(sync_engine: sa.Engine, sql: str, params: dict[str, Any]) -> list[Any]:
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        return list(conn.execute(sa.text(sql), params).all())


@pytest.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    """Motor async novo a cada teste.

    O engine e um singleton de modulo e o pool guarda conexoes amarradas ao event loop
    em que foram abertas; o pytest-asyncio cria um loop por teste. Reaproveitar conexao
    de loop fechado da "Event loop is closed" — e a falha aparece no teste *seguinte* ao
    que vazou, o que torna o sintoma dificil de ler.
    """
    await dispose_engine()
    yield
    await dispose_engine()


@pytest.fixture
async def ctx(settings: Settings) -> AsyncIterator[WorkerContext]:
    queue = FakeOutboundQueue()
    context: WorkerContext = {"settings": settings, "outbound": queue}
    yield context


def _turn(conversation: dict[str, Any]) -> AggregatedTurn:
    return AggregatedTurn(
        tenant_id=conversation["tenant_id"],
        conversation_id=conversation["conversation_id"],
        contact_id=conversation["contact_id"],
        parts=(
            InboundMessage(
                tenant_id=conversation["tenant_id"],
                conversation_id=conversation["conversation_id"],
                contact_id=conversation["contact_id"],
                channel="whatsapp",
                message_id=uuid.uuid4(),
                text="oi, queria marcar",
            ),
        ),
    )


async def test_estado_carregado_traz_historico_coletado_e_custo(
    sync_engine: sa.Engine, settings: Settings, conversation: dict[str, Any]
) -> None:
    _insert_messages(
        sync_engine, conversation, [("inbound", "oi"), ("outbound", "Oi! Como posso ajudar?")]
    )
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        conn.execute(
            sa.text(
                'UPDATE conversations SET collected = \'{"subject_name": "Mia"}\'::jsonb,'
                " stage = 'qualifying' WHERE id = :id"
            ),
            {"id": conversation["conversation_id"]},
        )

    loaded = await load_state(conversation["tenant_id"], conversation["conversation_id"], settings)

    assert loaded is not None
    assert loaded.state.message_count == 2
    assert [m.content for m in loaded.state.history] == ["oi", "Oi! Como posso ajudar?"]
    assert loaded.state.collected == {"subject_name": "Mia"}
    assert loaded.state.stage == "qualifying"
    assert loaded.channel == "whatsapp"


async def test_mensagens_da_mesma_transacao_saem_na_ordem_certa(
    sync_engine: sa.Engine, settings: Settings, conversation: dict[str, Any]
) -> None:
    """Rajada gravada numa transacao so nao pode voltar embaralhada.

    `now()` e o instante do inicio da transacao: com ele, as tres linhas ficariam com o
    mesmo `created_at` e — como a PK e UUID aleatorio — sem desempate possivel. A janela
    chegaria ao modelo com a resposta antes da pergunta. A migration 0005 troca o default
    por `clock_timestamp()`.
    """
    _insert_messages(
        sync_engine,
        conversation,
        [("inbound", "oi"), ("inbound", "queria marcar"), ("inbound", "pra sexta")],
    )

    loaded = await load_state(conversation["tenant_id"], conversation["conversation_id"], settings)

    assert loaded is not None
    assert [m.content for m in loaded.state.history] == ["oi", "queria marcar", "pra sexta"]


async def test_turno_persiste_resposta_com_tokens_e_custo(
    sync_engine: sa.Engine, ctx: WorkerContext, conversation: dict[str, Any]
) -> None:
    _insert_messages(sync_engine, conversation, [("inbound", "oi, queria marcar")])
    provider = ScriptedProvider(script=[text_response("Oi! Qual o nome do seu pet?")])
    handler = AgentTurnHandler(AgentEngine(provider=provider, extract=False))

    await handler(ctx, _turn(conversation))

    rows = _rows(
        sync_engine,
        "SELECT content, tokens_in, tokens_out, cost_usd, tool_calls FROM messages"
        " WHERE conversation_id = :id AND direction = 'outbound'",
        {"id": conversation["conversation_id"]},
    )
    assert len(rows) == 1
    assert rows[0].content == "Oi! Qual o nome do seu pet?"
    assert rows[0].tokens_in == 1000
    assert rows[0].tokens_out == 200
    assert rows[0].cost_usd == Decimal("0.004000")
    assert rows[0].tool_calls["model"] == "claude-sonnet-5"


async def test_turno_enfileira_a_resposta_com_a_chave_da_mensagem(
    sync_engine: sa.Engine, ctx: WorkerContext, conversation: dict[str, Any]
) -> None:
    """A chave de idempotencia e o id da linha: reentrega do job nao manda duas vezes."""
    _insert_messages(sync_engine, conversation, [("inbound", "oi")])
    handler = AgentTurnHandler(
        AgentEngine(provider=ScriptedProvider(script=[text_response("Oi!")]), extract=False)
    )

    await handler(ctx, _turn(conversation))

    queue: FakeOutboundQueue = ctx["outbound"]
    assert len(queue.sent) == 1
    enviado = queue.sent[0]
    assert enviado.text == "Oi!"

    rows = _rows(
        sync_engine,
        "SELECT id FROM messages WHERE conversation_id = :id AND direction = 'outbound'",
        {"id": conversation["conversation_id"]},
    )
    assert enviado.idempotency_key == str(rows[0].id)


async def test_turno_atualiza_collected_e_stage(
    sync_engine: sa.Engine, ctx: WorkerContext, conversation: dict[str, Any]
) -> None:
    _insert_messages(sync_engine, conversation, [("inbound", "e a gata Mia, primeira vez")])
    provider = ScriptedProvider(
        script=[text_response("Anotado! E como voce se chama?")],
        extraction={"subject_name": "Mia", "species": "gato", "is_first_visit": True},
    )
    handler = AgentTurnHandler(AgentEngine(provider=provider, extract=True))

    await handler(ctx, _turn(conversation))

    rows = _rows(
        sync_engine,
        "SELECT stage, collected, status FROM conversations WHERE id = :id",
        {"id": conversation["conversation_id"]},
    )
    assert rows[0].collected["subject_name"] == "Mia"
    assert rows[0].collected["species"] == "gato"
    # Intake ainda incompleto (faltam `service` e `contact_name`): nao vai para `offering`.
    assert rows[0].stage == "answering"
    assert rows[0].status == "active"


async def test_resumo_rolante_grava_texto_e_contador(
    sync_engine: sa.Engine, ctx: WorkerContext, conversation: dict[str, Any]
) -> None:
    """Com 15 mensagens novas desde o ultimo resumo, o turno regenera e anota ate onde foi."""
    _insert_messages(
        sync_engine,
        conversation,
        [("inbound" if i % 2 == 0 else "outbound", f"mensagem {i}") for i in range(15)],
    )
    provider = ScriptedProvider(
        script=[text_response("Resposta do turno"), text_response("Resumo da conversa ate aqui.")]
    )
    handler = AgentTurnHandler(AgentEngine(provider=provider, extract=False))

    await handler(ctx, _turn(conversation))

    rows = _rows(
        sync_engine,
        "SELECT summary, summary_message_count FROM conversations WHERE id = :id",
        {"id": conversation["conversation_id"]},
    )
    assert rows[0].summary == "Resumo da conversa ate aqui."
    assert rows[0].summary_message_count == 16  # 15 antigas + a resposta deste turno

    # O proximo turno nao regenera de novo: faltam 15 mensagens novas.
    loaded = await load_state(
        conversation["tenant_id"], conversation["conversation_id"], ctx["settings"]
    )
    assert loaded is not None
    assert loaded.state.summarized_message_count == 16


async def test_turno_de_tenant_inexistente_nao_estoura(ctx: WorkerContext) -> None:
    handler = AgentTurnHandler(AgentEngine(provider=ScriptedProvider()))
    turn = AggregatedTurn(
        tenant_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        parts=(),
    )
    await handler(ctx, turn)
    assert ctx["outbound"].sent == []
