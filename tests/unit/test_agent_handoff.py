"""Parte pura de `app.agent.handoff` (RF-26..RF-28, aceite da S10).

`should_run_turn` so toca banco no ramo de retomada por timeout; os outros dois ramos —
silencio dentro do prazo e conversa que nao esta em handoff — sao decisao pura e tem
teste aqui. O ramo de retomada de verdade (que fecha o handoff e muda `conversations`)
precisa de Postgres e mora em `tests/integration/test_handoff.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.agent.handoff import HandoffService, triggered_by_for

TENANT_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


# --------------------------------- triggered_by_for ---------------------------------


def test_motivo_nenhum_vira_rule() -> None:
    assert triggered_by_for(None) == "rule"


def test_motivo_de_tool_vira_agent() -> None:
    assert triggered_by_for("tool:fora_de_escopo") == "agent"


def test_motivo_de_gatilho_de_entrada_vira_contact() -> None:
    assert triggered_by_for("trigger:emergencia") == "contact"
    assert triggered_by_for("trigger:pedido_humano") == "contact"


def test_motivo_de_processo_do_motor_vira_agent() -> None:
    assert triggered_by_for("max_tool_iterations") == "agent"
    assert triggered_by_for("output_guardrail") == "agent"


def test_motivo_de_disjuntor_vira_rule() -> None:
    assert triggered_by_for("max_messages_per_conversation") == "rule"
    assert triggered_by_for("max_llm_cost_usd_per_conversation") == "rule"


# --------------------------------- should_run_turn ---------------------------------


async def test_conversa_ativa_sempre_roda() -> None:
    service = HandoffService()
    pode = await service.should_run_turn(
        tenant_id=TENANT_ID,
        conversation_id=CONVERSATION_ID,
        status="active",
        silenced_until=None,
        now=NOW,
    )
    assert pode is True


async def test_handoff_dentro_do_prazo_fica_em_silencio() -> None:
    service = HandoffService()
    pode = await service.should_run_turn(
        tenant_id=TENANT_ID,
        conversation_id=CONVERSATION_ID,
        status="handoff",
        silenced_until=NOW + timedelta(minutes=30),
        now=NOW,
    )
    assert pode is False


async def test_handoff_sem_prazo_gravado_fica_em_silencio() -> None:
    """Sem `silenced_until`, so a retomada manual (painel) resolve — nunca automatico."""
    service = HandoffService()
    pode = await service.should_run_turn(
        tenant_id=TENANT_ID,
        conversation_id=CONVERSATION_ID,
        status="handoff",
        silenced_until=None,
        now=NOW,
    )
    assert pode is False
