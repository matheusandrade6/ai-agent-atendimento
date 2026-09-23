"""Tool `escalate_to_human` (secao 11.4, RF-26, aceite da S10).

Sem banco: o handler so sinaliza a escalada no `ToolResult` — quem abre o handoff de
verdade e `app.agent.handoff.HandoffService`, chamado por `app.agent.runner` depois que
o turno termina. O teste de idempotencia por conversa mora aqui porque a chave e
propriedade da tool, mas a prova de que ela evita dois handoffs esta na integracao
(`tests/integration/test_handoff.py`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.agent.tools.base import ToolContext
from app.agent.tools.escalate_to_human import EscalateToHumanArgs, escalate_to_human_tool
from app.domain.tenant_config import TenantConfig, load_tenant_config

TENANTS_DIR = Path(__file__).resolve().parents[2] / "config" / "tenants"
SAO_PAULO = ZoneInfo("America/Sao_Paulo")

TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
CONTACT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture(scope="module")
def config() -> TenantConfig:
    return load_tenant_config(TENANTS_DIR / "clinica-exemplo.yaml")


def _ctx(config: TenantConfig) -> ToolContext:
    return ToolContext(
        tenant_id=TENANT_ID,
        conversation_id=CONVERSATION_ID,
        contact_id=CONTACT_ID,
        channel="whatsapp",
        config=config,
        now=datetime(2026, 9, 9, 10, 0, tzinfo=SAO_PAULO),
    )


def test_schema_nao_expoe_tenant_id() -> None:
    tool = escalate_to_human_tool()
    assert "tenant_id" not in tool.input_schema["properties"]
    assert set(tool.input_schema["required"]) == {"category", "reason"}


def test_e_tool_de_escrita() -> None:
    assert escalate_to_human_tool().kind == "write"


async def test_handler_devolve_categoria_e_motivo(config: TenantConfig) -> None:
    tool = escalate_to_human_tool()
    result = await tool.handler(
        _ctx(config), {"category": "pedido_explicito", "reason": "cliente pediu atendente"}
    )

    assert result.is_error is False
    assert result.content == {
        "status": "escalated",
        "category": "pedido_explicito",
        "reason": "cliente pediu atendente",
    }
    assert result.entity_id == CONVERSATION_ID


def test_chave_de_idempotencia_ignora_argumentos(config: TenantConfig) -> None:
    """Duas chamadas na mesma conversa colapsam numa so — nunca dois handoffs."""
    tool = escalate_to_human_tool()
    ctx = _ctx(config)
    chave_1 = tool.key_for(ctx, {"category": "outro", "reason": "primeiro motivo"})
    chave_2 = tool.key_for(ctx, {"category": "baixa_confianca", "reason": "motivo diferente"})
    assert chave_1 == chave_2


def test_chave_de_idempotencia_muda_por_conversa(config: TenantConfig) -> None:
    tool = escalate_to_human_tool()
    outra_conversa = ToolContext(
        tenant_id=TENANT_ID,
        conversation_id=uuid.uuid4(),
        contact_id=CONTACT_ID,
        channel="whatsapp",
        config=config,
        now=datetime(2026, 9, 9, 10, 0, tzinfo=SAO_PAULO),
    )
    chave_1 = tool.key_for(_ctx(config), {"category": "outro", "reason": "x"})
    chave_2 = tool.key_for(outra_conversa, {"category": "outro", "reason": "x"})
    assert chave_1 != chave_2


def test_categoria_invalida_nao_valida() -> None:
    with pytest.raises(ValidationError):
        EscalateToHumanArgs.model_validate({"category": "nao_existe", "reason": "motivo valido"})


def test_motivo_curto_demais_nao_valida() -> None:
    with pytest.raises(ValidationError):
        EscalateToHumanArgs.model_validate({"category": "outro", "reason": "x"})
