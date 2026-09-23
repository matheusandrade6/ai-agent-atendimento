"""Handoff ponta a ponta com banco real (RF-26..RF-30, aceite da Sessao S10).

Tres coisas sob teste, e as tres so o Postgres prova: que o worker inbound **nao chama
o LLM** com handoff ativo dentro do prazo (RF-28), que a retomada por timeout volta a
chamar o motor e fecha a linha de `handoffs`, e que a notificacao sai **uma unica vez**
por escalada (RF-27) mesmo que o handoff seja consultado de novo depois.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from app.agent.audit import NullAuditSink
from app.agent.engine import AgentEngine
from app.agent.handoff import HandoffService, WhatsAppHandoffNotifier
from app.agent.runner import AgentTurnHandler
from app.agent.tools.base import ToolRegistry
from app.agent.tools.escalate_to_human import escalate_to_human_tool
from app.channels.base import SentMessage
from app.core.config import Settings
from app.core.db import dispose_engine
from app.core.time import now_utc
from app.domain.tenant_config import load_tenant_config
from app.workers.aggregator import AggregatedTurn
from app.workers.base import WorkerContext
from app.workers.queue import InboundMessage, OutboundMessage
from tests.fakes import ScriptedProvider, text_response, tool_response

TENANTS_DIR = Path(__file__).resolve().parents[2] / "config" / "tenants"

pytestmark = pytest.mark.integration

WA_ID = "5511977778888"
RESPONSAVEL = "+5511999999999"  # config/tenants/clinica-exemplo.yaml, escalation.notify


class FakeOutboundQueue:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def enqueue_outbound(
        self, message: OutboundMessage, *, defer_seconds: float = 0.0
    ) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        return None


class FakeChannel:
    """Canal de saida falso: so registra o que foi enviado, sempre com sucesso."""

    def __init__(self) -> None:
        self.enviados: list[tuple[str, str]] = []

    async def send_text(self, *, to: str, text: str) -> SentMessage:
        self.enviados.append((to, text))
        return SentMessage(provider_msg_id=f"wamid.{uuid.uuid4().hex[:8]}")

    async def mark_read(self, *, provider_msg_id: str, typing: bool = False) -> None:
        return None

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    """Ver docstring identica em `tests/integration/test_agent_turn.py`."""
    await dispose_engine()
    yield
    await dispose_engine()


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
        conn.execute(
            sa.text(
                "INSERT INTO messages (tenant_id, conversation_id, direction, author, content)"
                " VALUES (:t, :c, 'inbound', 'contact', :content)"
            ),
            {
                "t": tenant_whatsapp["tenant_id"],
                "c": conversation_id,
                "content": "oi, preciso de ajuda",
            },
        )
    yield {"conversation_id": conversation_id, **tenant_whatsapp}


def _set_handoff_state(
    sync_engine: sa.Engine, conversation_id: uuid.UUID, *, status: str, silenced_until: Any
) -> None:
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        conn.execute(
            sa.text(
                "UPDATE conversations SET status = :status, silenced_until = :until WHERE id = :id"
            ),
            {"status": status, "until": silenced_until, "id": conversation_id},
        )


def _insert_open_handoff(
    sync_engine: sa.Engine, conversation: dict[str, Any], *, reason: str = "trigger:emergencia"
) -> uuid.UUID:
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        handoff_id: uuid.UUID = conn.execute(
            sa.text(
                "INSERT INTO handoffs (tenant_id, conversation_id, reason, triggered_by)"
                " VALUES (:t, :c, :reason, 'rule') RETURNING id"
            ),
            {
                "t": conversation["tenant_id"],
                "c": conversation["conversation_id"],
                "reason": reason,
            },
        ).scalar_one()
        return handoff_id


def _conversation_row(sync_engine: sa.Engine, conversation_id: uuid.UUID) -> Any:
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        return conn.execute(
            sa.text("SELECT status, silenced_until FROM conversations WHERE id = :id"),
            {"id": conversation_id},
        ).one()


def _handoff_rows(sync_engine: sa.Engine, conversation_id: uuid.UUID) -> list[Any]:
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        return list(
            conn.execute(
                sa.text(
                    "SELECT id, reason, triggered_by, notified_at, closed_at FROM handoffs"
                    " WHERE conversation_id = :id ORDER BY opened_at"
                ),
                {"id": conversation_id},
            ).all()
        )


@pytest.fixture
def ctx(settings: Settings) -> WorkerContext:
    channel = FakeChannel()
    return {
        "settings": settings,
        "outbound": FakeOutboundQueue(),
        "channel_factory": lambda _ctx, _config: channel,
        "_fake_channel": channel,  # atalho para o teste ler o que foi enviado
    }


def _turn(conversation: dict[str, Any], text: str = "oi, preciso de ajuda") -> AggregatedTurn:
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
                text=text,
            ),
        ),
    )


# ------------------------- RF-28: silencio com handoff ativo -------------------------


async def test_handoff_ativo_dentro_do_prazo_nao_chama_o_llm(
    sync_engine: sa.Engine, ctx: WorkerContext, conversation: dict[str, Any]
) -> None:
    _set_handoff_state(
        sync_engine,
        conversation["conversation_id"],
        status="handoff",
        silenced_until=now_utc() + timedelta(hours=1),
    )
    provider = ScriptedProvider(script=[text_response("nao deveria ser chamado")])
    handler = AgentTurnHandler(AgentEngine(provider=provider, extract=False))

    await handler(ctx, _turn(conversation))

    assert provider.calls == []
    queue: FakeOutboundQueue = ctx["outbound"]
    assert queue.sent == []
    # O handoff continua exatamente como estava: nada foi retomado antes da hora.
    row = _conversation_row(sync_engine, conversation["conversation_id"])
    assert row.status == "handoff"


async def test_retomada_por_timeout_volta_a_chamar_o_llm_e_fecha_o_handoff(
    sync_engine: sa.Engine, ctx: WorkerContext, conversation: dict[str, Any]
) -> None:
    _insert_open_handoff(sync_engine, conversation)
    _set_handoff_state(
        sync_engine,
        conversation["conversation_id"],
        status="handoff",
        silenced_until=now_utc() - timedelta(minutes=1),
    )
    provider = ScriptedProvider(script=[text_response("Voltei! Como posso ajudar?")])
    handler = AgentTurnHandler(
        AgentEngine(provider=provider, extract=False), handoff=HandoffService()
    )

    await handler(ctx, _turn(conversation, text="ainda esta ai?"))

    assert len(provider.calls) == 1
    row = _conversation_row(sync_engine, conversation["conversation_id"])
    assert row.status == "active"
    assert row.silenced_until is None
    handoffs = _handoff_rows(sync_engine, conversation["conversation_id"])
    assert len(handoffs) == 1
    assert handoffs[0].closed_at is not None


# ------------------------ RF-26/27: abertura, silencio e aviso ------------------------


async def test_escalate_to_human_abre_handoff_silencia_e_notifica_uma_vez(
    sync_engine: sa.Engine, ctx: WorkerContext, conversation: dict[str, Any]
) -> None:
    audit = NullAuditSink()
    provider = ScriptedProvider(
        script=[
            tool_response(
                "escalate_to_human",
                {"category": "fora_de_escopo", "reason": "pergunta que a base nao cobre"},
            ),
            text_response("Vou chamar alguem da equipe para te ajudar."),
        ]
    )
    engine = AgentEngine(
        provider=provider,
        tools=ToolRegistry((escalate_to_human_tool(),)),
        audit=audit,
        extract=False,
    )
    handler = AgentTurnHandler(engine, handoff=HandoffService(audit=audit))

    await handler(ctx, _turn(conversation))

    row = _conversation_row(sync_engine, conversation["conversation_id"])
    assert row.status == "handoff"
    assert row.silenced_until is not None
    # Default de `handoff_silence_minutes` no tenant de exemplo e 120 minutos.
    assert row.silenced_until > now_utc() + timedelta(minutes=110)

    handoffs = _handoff_rows(sync_engine, conversation["conversation_id"])
    assert len(handoffs) == 1
    assert handoffs[0].reason == "tool:pergunta que a base nao cobre"
    assert handoffs[0].triggered_by == "agent"
    assert handoffs[0].notified_at is not None

    channel: FakeChannel = ctx["_fake_channel"]
    assert len(channel.enviados) == 1  # exatamente um envio (RF-27)
    destino, texto = channel.enviados[0]
    assert destino == RESPONSAVEL
    assert "pergunta que a base nao cobre" in texto

    acoes = [entry.action for entry in audit.entries]
    assert acoes.count("handoff.opened") == 1
    assert acoes.count("handoff.notified") == 1


async def test_segunda_chamada_de_open_nao_duplica_nem_renotifica(
    sync_engine: sa.Engine, conversation: dict[str, Any]
) -> None:
    """Idempotencia de `HandoffService.open`, testada direto (sem passar pelo turno)."""
    config = load_tenant_config(TENANTS_DIR / "clinica-exemplo.yaml").resolved(
        {"WA_PHONE_NUMBER_ID": "x", "WA_WABA_ID": "y"}
    )

    audit = NullAuditSink()
    service = HandoffService(audit=audit)
    channel = FakeChannel()
    notifier = WhatsAppHandoffNotifier(channel)
    now = now_utc()

    primeira = await service.open(
        tenant_id=conversation["tenant_id"],
        conversation_id=conversation["conversation_id"],
        config=config,
        reason="trigger:emergencia",
        triggered_by="contact",
        summary="resumo",
        now=now,
        notifier=notifier,
    )
    segunda = await service.open(
        tenant_id=conversation["tenant_id"],
        conversation_id=conversation["conversation_id"],
        config=config,
        reason="trigger:baixa_confianca",
        triggered_by="rule",
        summary="outro resumo",
        now=now,
        notifier=notifier,
    )

    assert primeira.opened is True
    assert primeira.notified is True
    assert segunda.opened is False
    assert segunda.notified is False
    assert segunda.handoff_id == primeira.handoff_id
    assert len(channel.enviados) == 1

    handoffs = _handoff_rows(sync_engine, conversation["conversation_id"])
    assert len(handoffs) == 1
