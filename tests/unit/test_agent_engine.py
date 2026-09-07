"""Loop de tool calling (secao 11.1) — testado com tool falsa e modelo roteirizado.

O que estes testes protegem, em ordem de gravidade:

1. Escrita repetida no mesmo turno **nao executa duas vezes** (invariante 5).
2. `tenant_id` vindo nos argumentos **nunca** chega ao handler (invariante 3).
3. `MAX_TOOL_ITERATIONS` e teto de verdade, e o turno ainda termina com texto.
4. Custo e tokens somam **todas** as chamadas — senao o disjuntor da 11.5 dorme.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.agent.audit import NullAuditSink
from app.agent.engine import (
    MAX_TOOL_ITERATIONS,
    AgentEngine,
    ConversationState,
    TurnRequest,
    next_stage,
)
from app.agent.memory import StoredMessage
from app.agent.tools.base import ToolContext, ToolError, ToolRegistry, ToolResult, ToolSpec
from app.domain.tenant_config import TenantConfig, load_tenant_config
from tests.fakes import RecordingTool, ScriptedProvider, text_response, tool_response

TENANTS_DIR = Path(__file__).resolve().parents[2] / "config" / "tenants"
SAO_PAULO = ZoneInfo("America/Sao_Paulo")

TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
CONTACT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture(scope="module")
def config() -> TenantConfig:
    return load_tenant_config(TENANTS_DIR / "clinica-exemplo.yaml")


def make_request(config: TenantConfig, **state: object) -> TurnRequest:
    return TurnRequest(
        tenant_id=TENANT_ID,
        conversation_id=CONVERSATION_ID,
        contact_id=CONTACT_ID,
        channel="whatsapp",
        config=config,
        now=datetime(2026, 9, 9, 10, 0, tzinfo=SAO_PAULO),
        state=ConversationState(
            history=(StoredMessage("inbound", "contact", "oi, queria marcar"),),
            **state,  # type: ignore[arg-type]
        ),
    )


def make_engine(provider: ScriptedProvider, *specs: ToolSpec, **kwargs: object) -> AgentEngine:
    return AgentEngine(
        provider=provider,
        tools=ToolRegistry(specs),
        audit=NullAuditSink(),
        extract=False,
        **kwargs,  # type: ignore[arg-type]
    )


# ------------------------------- caminho feliz -------------------------------


async def test_turno_sem_tool_devolve_o_texto(config: TenantConfig) -> None:
    provider = ScriptedProvider(script=[text_response("Oi! Qual o nome do seu pet?")])
    outcome = await make_engine(provider).run_turn(make_request(config))

    assert outcome.reply == "Oi! Qual o nome do seu pet?"
    assert outcome.tool_calls == ()
    assert outcome.iterations == 1


async def test_tool_de_leitura_roda_e_o_resultado_volta_ao_modelo(config: TenantConfig) -> None:
    tool = RecordingTool(name="check_availability", result={"slots": [{"human": "14h"}]})
    provider = ScriptedProvider(
        script=[
            tool_response("check_availability", {"service_id": "svc-1"}),
            text_response("Tenho as 14h. Serve?"),
        ]
    )
    outcome = await make_engine(provider, tool.spec()).run_turn(make_request(config))

    assert len(tool.executions) == 1
    assert outcome.reply == "Tenho as 14h. Serve?"
    assert outcome.used_tools == ("check_availability",)
    # O resultado voltou como `tool_result` na mensagem seguinte do usuario.
    ultima = provider.last_call.messages[-1]
    assert ultima.role == "user"
    assert "slots" in str(ultima.content)


# ---------------------------- invariante 5 (escrita) ----------------------------


async def test_escrita_repetida_no_mesmo_turno_nao_executa_duas_vezes(
    config: TenantConfig,
) -> None:
    """O modelo insiste; a tool nao roda de novo — devolve o resultado da primeira."""
    tool = RecordingTool(name="confirm_appointment", result={"status": "confirmed", "id": "ap-1"})
    args = {"hold_id": "hold-1", "subject_name": "Mia"}
    provider = ScriptedProvider(
        script=[
            tool_response("confirm_appointment", args, call_id="tu_1"),
            tool_response("confirm_appointment", args, call_id="tu_2"),
            text_response("Prontinho!"),
        ]
    )
    outcome = await make_engine(provider, tool.spec(kind="write")).run_turn(make_request(config))

    assert len(tool.executions) == 1
    assert len(outcome.tool_calls) == 2
    assert outcome.tool_calls[1].deduplicated is True
    # A segunda chamada devolve exatamente o mesmo resultado da primeira.
    assert outcome.tool_calls[1].result == outcome.tool_calls[0].result


async def test_escrita_com_argumentos_diferentes_executa_de_novo(config: TenantConfig) -> None:
    """A protecao e por chave de idempotencia, nao por nome da tool."""
    tool = RecordingTool(name="save_contact_info")
    provider = ScriptedProvider(
        script=[
            tool_response("save_contact_info", {"name": "Carla"}),
            tool_response("save_contact_info", {"name": "Mia"}),
            text_response("Anotado."),
        ]
    )
    await make_engine(provider, tool.spec(kind="write")).run_turn(make_request(config))
    assert len(tool.executions) == 2


async def test_ordem_dos_argumentos_nao_burla_a_idempotencia(config: TenantConfig) -> None:
    """Mesmo pedido com as chaves em outra ordem continua sendo o mesmo pedido."""
    tool = RecordingTool(name="hold_slot")
    provider = ScriptedProvider(
        script=[
            tool_response("hold_slot", {"a": 1, "b": 2}),
            tool_response("hold_slot", {"b": 2, "a": 1}),
            text_response("ok"),
        ]
    )
    await make_engine(provider, tool.spec(kind="write")).run_turn(make_request(config))
    assert len(tool.executions) == 1


async def test_escrita_que_falhou_pode_ser_tentada_de_novo(config: TenantConfig) -> None:
    """So o sucesso e memorizado: repetir uma escrita que falhou e o comportamento certo."""
    tool = RecordingTool(name="hold_slot", fail=True)
    provider = ScriptedProvider(
        script=[
            tool_response("hold_slot", {"slot_token": "x"}),
            tool_response("hold_slot", {"slot_token": "x"}),
            text_response("Nao consegui reservar."),
        ]
    )
    await make_engine(provider, tool.spec(kind="write")).run_turn(make_request(config))
    assert len(tool.executions) == 2


async def test_leitura_repetida_nao_e_bloqueada(config: TenantConfig) -> None:
    tool = RecordingTool(name="list_services")
    provider = ScriptedProvider(
        script=[
            tool_response("list_services", {}),
            tool_response("list_services", {}),
            text_response("ok"),
        ]
    )
    await make_engine(provider, tool.spec()).run_turn(make_request(config))
    assert len(tool.executions) == 2


# ---------------------------- invariante 3 (tenant) ----------------------------


async def test_tenant_id_dos_argumentos_nunca_chega_ao_handler(config: TenantConfig) -> None:
    tool = RecordingTool(name="list_services")
    provider = ScriptedProvider(
        script=[
            tool_response("list_services", {"tenant_id": "outro-tenant", "q": "vacina"}),
            text_response("ok"),
        ]
    )
    await make_engine(provider, tool.spec()).run_turn(make_request(config))

    assert tool.executions == [{"q": "vacina"}]


async def test_tenant_do_contexto_e_o_que_a_tool_enxerga(config: TenantConfig) -> None:
    visto: list[ToolContext] = []

    async def handler(ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        visto.append(ctx)
        return ToolResult(content={"ok": True})

    spec = ToolSpec(
        name="list_services",
        description="catalogo",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
    )
    provider = ScriptedProvider(script=[tool_response("list_services", {}), text_response("ok")])
    await make_engine(provider, spec).run_turn(make_request(config))

    assert visto[0].tenant_id == TENANT_ID
    assert visto[0].conversation_id == CONVERSATION_ID


# ------------------------------ teto de iteracoes ------------------------------


async def test_teto_de_iteracoes_e_respeitado_e_o_turno_fecha_com_texto(
    config: TenantConfig,
) -> None:
    """Modelo que nunca para: o motor corta e ainda entrega resposta a pessoa."""
    tool = RecordingTool(name="check_availability")
    provider = ScriptedProvider(
        script=[
            *(tool_response("check_availability", {"n": i}) for i in range(MAX_TOOL_ITERATIONS)),
            text_response("Vou confirmar com a equipe e te aviso."),
        ]
    )
    engine = make_engine(provider, tool.spec())
    outcome = await engine.run_turn(make_request(config))

    assert outcome.iterations == MAX_TOOL_ITERATIONS
    assert len(tool.executions) == MAX_TOOL_ITERATIONS
    assert outcome.escalate is True
    assert outcome.escalation_reason == "max_tool_iterations"
    # A ultima chamada foi sem tools: o modelo e obrigado a fechar com texto.
    assert provider.last_call.tools == ()
    assert outcome.reply == "Vou confirmar com a equipe e te aviso."


async def test_teto_configuravel_por_motor(config: TenantConfig) -> None:
    tool = RecordingTool(name="check_availability")
    provider = ScriptedProvider(script=[tool_response("check_availability", {})])
    engine = make_engine(provider, tool.spec(), max_iterations=2)
    outcome = await engine.run_turn(make_request(config))
    assert outcome.iterations == 2
    assert len(tool.executions) == 2


# --------------------------------- erros ---------------------------------


async def test_tool_desconhecida_volta_como_erro_e_o_turno_segue(config: TenantConfig) -> None:
    provider = ScriptedProvider(
        script=[tool_response("tool_inventada", {}), text_response("Deixa comigo.")]
    )
    outcome = await make_engine(provider).run_turn(make_request(config))

    assert outcome.reply == "Deixa comigo."
    assert outcome.tool_calls[0].ok is False
    assert outcome.tool_calls[0].result["error"] == "ferramenta_desconhecida"


async def test_excecao_inesperada_nao_vaza_detalhe_para_o_modelo(config: TenantConfig) -> None:
    async def explode(ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        raise RuntimeError("senha do banco: hunter2")

    spec = ToolSpec(
        name="check_availability",
        description="slots",
        input_schema={"type": "object", "properties": {}},
        handler=explode,
    )
    provider = ScriptedProvider(
        script=[tool_response("check_availability", {}), text_response("Vou verificar.")]
    )
    outcome = await make_engine(provider, spec).run_turn(make_request(config))

    assert outcome.tool_calls[0].result == {"error": "falha_interna"}
    assert "hunter2" not in str(provider.last_call.messages)


async def test_tool_error_devolve_mensagem_util_ao_modelo(config: TenantConfig) -> None:
    async def recusa(ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        raise ToolError("hold expirado")

    spec = ToolSpec(
        name="confirm_appointment",
        description="confirma",
        input_schema={"type": "object", "properties": {}},
        handler=recusa,
        kind="write",
    )
    provider = ScriptedProvider(
        script=[tool_response("confirm_appointment", {}), text_response("O horario expirou.")]
    )
    outcome = await make_engine(provider, spec).run_turn(make_request(config))
    assert outcome.tool_calls[0].result["detail"] == "hold expirado"


async def test_argumentos_invalidos_voltam_ao_modelo_para_correcao(config: TenantConfig) -> None:
    from pydantic import BaseModel

    class Args(BaseModel):
        service_id: str

    tool = RecordingTool(name="check_availability")
    provider = ScriptedProvider(
        script=[
            tool_response("check_availability", {"servico": "consulta"}),
            text_response("Qual servico?"),
        ]
    )
    engine = make_engine(provider, tool.spec(args_model=Args))
    outcome = await engine.run_turn(make_request(config))

    assert tool.executions == []
    assert outcome.tool_calls[0].result["error"] == "argumentos_invalidos"
    assert "service_id" in outcome.tool_calls[0].result["detail"]


# --------------------------------- auditoria ---------------------------------


async def test_toda_chamada_de_tool_vira_linha_de_auditoria(config: TenantConfig) -> None:
    tool = RecordingTool(name="confirm_appointment", kind="write")
    audit = NullAuditSink()
    provider = ScriptedProvider(
        script=[
            tool_response("confirm_appointment", {"hold_id": "h1"}),
            tool_response("confirm_appointment", {"hold_id": "h1"}),
            text_response("Prontinho!"),
        ]
    )
    engine = AgentEngine(
        provider=provider,
        tools=ToolRegistry((tool.spec(kind="write"),)),
        audit=audit,
        extract=False,
    )
    await engine.run_turn(make_request(config))

    assert len(audit.entries) == 2
    assert all(e.tenant_id == TENANT_ID for e in audit.entries)
    assert all(e.action == "tool.confirm_appointment" for e in audit.entries)
    # A insistencia do modelo fica registrada: e sinal, nao ruido.
    assert audit.entries[1].payload is not None
    assert audit.entries[1].payload["deduplicated"] is True


# ------------------------------ tokens e custo ------------------------------


async def test_custo_soma_todas_as_chamadas_do_turno(config: TenantConfig) -> None:
    tool = RecordingTool(name="check_availability")
    provider = ScriptedProvider(
        script=[tool_response("check_availability", {}), text_response("Tenho as 14h.")]
    )
    outcome = await make_engine(provider, tool.spec()).run_turn(make_request(config))

    assert outcome.cost_usd == Decimal("0.008")  # duas chamadas de 0.004
    assert outcome.usage.output_tokens == 400
    assert outcome.usage.total_input_tokens == 2000


async def test_custo_inclui_a_chamada_de_extracao(config: TenantConfig) -> None:
    """Extracao e chamada de LLM: fora da conta, o disjuntor de custo mede menos."""
    provider = ScriptedProvider(script=[text_response("Qual o nome do pet?")], extraction={})
    engine = AgentEngine(provider=provider, extract=True)
    outcome = await engine.run_turn(make_request(config))

    assert provider.extraction_calls == 1
    assert outcome.cost_usd == Decimal("0.0045")


# ------------------------------ disjuntor (11.5) ------------------------------


async def test_conversa_longa_demais_escala_sem_chamar_o_modelo(config: TenantConfig) -> None:
    provider = ScriptedProvider(script=[text_response("nao deveria ser chamado")])
    request = make_request(config, message_count=config.limits.max_messages_per_conversation + 1)
    outcome = await make_engine(provider).run_turn(request)

    assert provider.calls == []
    assert outcome.escalate is True
    assert outcome.escalation_reason == "max_messages_per_conversation"
    assert outcome.stage == "handoff"


async def test_conversa_cara_demais_escala_sem_chamar_o_modelo(config: TenantConfig) -> None:
    provider = ScriptedProvider(script=[text_response("nao deveria ser chamado")])
    caro = Decimal(str(config.limits.max_llm_cost_usd_per_conversation)) + Decimal("0.01")
    outcome = await make_engine(provider).run_turn(make_request(config, cost_so_far_usd=caro))

    assert provider.calls == []
    assert outcome.escalation_reason == "max_llm_cost_usd_per_conversation"


# --------------------------------- estagio ---------------------------------


@pytest.mark.parametrize(
    ("atual", "tools", "completo", "esperado"),
    [
        ("greeting", (), False, "answering"),
        ("greeting", (), True, "offering"),
        ("qualifying", ("check_availability",), False, "offering"),
        ("offering", ("hold_slot",), True, "confirming"),
        ("confirming", ("confirm_appointment",), True, "booked"),
        ("booked", ("reschedule_appointment",), True, "booked"),
        ("qualifying", ("escalate_to_human",), False, "handoff"),
        ("answering", (), False, "answering"),
    ],
)
def test_estagio_e_decidido_em_codigo(
    atual: str, tools: tuple[str, ...], completo: bool, esperado: str
) -> None:
    assert (
        next_stage(atual, successful_tools=tools, intake_complete=completo, escalated=False)
        == esperado
    )


def test_escalonamento_ganha_de_tudo() -> None:
    assert (
        next_stage(
            "confirming",
            successful_tools=("confirm_appointment",),
            intake_complete=True,
            escalated=True,
        )
        == "handoff"
    )


def test_tool_que_falhou_nao_muda_o_estagio(config: TenantConfig) -> None:
    """`booked` so depois de `confirm_appointment` **bem-sucedida** (invariante 1)."""
    assert (
        next_stage("confirming", successful_tools=(), intake_complete=True, escalated=False)
        == "offering"
    )


# ------------------------------ prompt do turno ------------------------------


async def test_prompt_do_turno_recebe_o_missing_calculado(config: TenantConfig) -> None:
    """O que o modelo le sobre o intake vem do codigo, nao da resposta anterior dele."""
    provider = ScriptedProvider(script=[text_response("Qual o nome do pet?")])
    request = make_request(config, collected={"subject_name": "Mia"})
    await make_engine(provider).run_turn(request)

    assert "Ainda falta: species, is_first_visit, service, contact_name" in provider.system_text


async def test_janela_enviada_ao_modelo_traz_a_mensagem_delimitada(config: TenantConfig) -> None:
    provider = ScriptedProvider(script=[text_response("oi")])
    await make_engine(provider).run_turn(make_request(config))

    primeira = provider.last_call.messages[0]
    assert primeira.role == "user"
    assert '<dado tipo="mensagem">' in primeira.text
