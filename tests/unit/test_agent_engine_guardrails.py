"""Os guardrails dentro do loop do motor (passos 3 e 8 da 11.1).

O teste do guardrail isolado esta em `tests/unit/test_guardrails.py`. Aqui esta sob
teste o que so o motor pode provar, que e justamente o criterio de aceite da S09:

1. Gatilho de entrada **nao chega ao modelo** — o LLM nem e chamado.
2. Resposta com horario que nao veio de tool e **descartada e regenerada**.
3. Se a segunda resposta tambem falhar, o turno **escala** e o texto do modelo nao sai.
4. O custo da regeneracao entra na conta do turno — senao o disjuntor da 11.5 dorme.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.agent.audit import NullAuditSink
from app.agent.engine import AgentEngine, ConversationState, TurnRequest, last_inbound_text
from app.agent.guardrails import InputGuardrail, OutputGuardrail
from app.agent.memory import StoredMessage
from app.agent.tools.base import ToolContext, ToolRegistry, ToolResult, ToolSpec
from app.domain.tenant_config import TenantConfig, load_tenant_config
from tests.fakes import ScriptedProvider, text_response, tool_response

TENANTS_DIR = Path(__file__).resolve().parents[2] / "config" / "tenants"
SAO_PAULO = ZoneInfo("America/Sao_Paulo")

TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
CONTACT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
#: Segunda-feira, 7 de setembro de 2026.
NOW = datetime(2026, 9, 7, 10, 0, tzinfo=SAO_PAULO)


@pytest.fixture(scope="module")
def config() -> TenantConfig:
    return load_tenant_config(TENANTS_DIR / "clinica-exemplo.yaml")


def make_request(config: TenantConfig, *entradas: str) -> TurnRequest:
    history = tuple(StoredMessage("inbound", "contact", texto) for texto in entradas)
    return TurnRequest(
        tenant_id=TENANT_ID,
        conversation_id=CONVERSATION_ID,
        contact_id=CONTACT_ID,
        channel="whatsapp",
        config=config,
        now=NOW,
        state=ConversationState(history=history),
    )


def make_engine(provider: ScriptedProvider, *specs: ToolSpec) -> AgentEngine:
    return AgentEngine(
        provider=provider,
        tools=ToolRegistry(specs),
        audit=NullAuditSink(),
        extract=False,
        input_guardrail=InputGuardrail(),
        output_guardrail=OutputGuardrail(),
    )


def availability_tool(*slots: str) -> ToolSpec:
    """`check_availability` falsa, devolvendo instantes ISO como a real devolve."""

    async def handler(ctx: ToolContext, arguments: object) -> ToolResult:
        return ToolResult(
            content={
                "slots": [
                    {"slot_token": f"tok{index}", "starts_at": starts, "provider_name": "Dra. Ana"}
                    for index, starts in enumerate(slots)
                ]
            }
        )

    return ToolSpec(
        name="check_availability",
        description="horarios reais",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
        kind="read",
    )


# ============================== passo 3: entrada ==============================


async def test_gatilho_de_emergencia_nao_chega_ao_modelo(config: TenantConfig) -> None:
    provider = ScriptedProvider(script=[text_response("nao deveria ser chamado")])
    outcome = await make_engine(provider).run_turn(
        make_request(config, "socorro", "meu cachorro foi atropelado")
    )

    assert provider.calls == []
    assert outcome.escalate
    assert outcome.escalation_reason == "trigger:emergencia"
    assert outcome.stage == "handoff"
    assert "emergencia" in outcome.reply.lower()
    assert outcome.cost_usd == Decimal(0)


async def test_gatilho_le_a_rajada_inteira_e_nao_so_o_ultimo_balao(
    config: TenantConfig,
) -> None:
    """Quem esta desesperado escreve em varios baloes; o gatilho precisa ver todos."""
    state = ConversationState(
        history=(
            StoredMessage("outbound", "agent", "Oi! Como posso ajudar?"),
            StoredMessage("inbound", "contact", "socorro"),
            StoredMessage("inbound", "contact", "ele esta com sangramento"),
        )
    )
    assert last_inbound_text(state) == "socorro\nele esta com sangramento"


async def test_pedido_de_humano_curto_circuita_sem_escalar_o_custo(
    config: TenantConfig,
) -> None:
    provider = ScriptedProvider(script=[text_response("nao deveria ser chamado")])
    outcome = await make_engine(provider).run_turn(
        make_request(config, "quero falar com um atendente")
    )

    assert provider.calls == []
    assert outcome.escalate
    assert outcome.usage.total_input_tokens == 0


async def test_o_motor_preenche_os_campos_da_unidade_no_texto_do_gatilho(
    config: TenantConfig,
) -> None:
    """DEF-01 ponta a ponta: e o motor que junta config e turno e passa os `placeholders`.

    A mensagem sob teste e a que manda a pessoa sair de casa com o animal ferido. O que ela
    precisa levar e justamente o endereco e o telefone.
    """
    provider = ScriptedProvider(script=[text_response("nao deveria ser chamado")])
    outcome = await make_engine(provider).run_turn(
        make_request(config, "socorro meu cachorro foi atropelado")
    )

    assert config.identity.address in outcome.reply
    assert config.identity.phone in outcome.reply
    assert not any("{" in mensagem for mensagem in outcome.messages)


async def test_pedido_de_humano_nao_abre_handoff_calado(config: TenantConfig) -> None:
    """DEF-02 ponta a ponta: o handoff abre e o cliente e avisado no mesmo turno.

    Sem o aviso, por RF-28 a conversa fica muda tambem nas mensagens seguintes — a pessoa
    repete o pedido no vazio e desiste.
    """
    provider = ScriptedProvider(script=[text_response("nao deveria ser chamado")])
    outcome = await make_engine(provider).run_turn(
        make_request(config, "quero falar com um atendente")
    )

    assert outcome.escalate
    assert outcome.escalation_reason == "trigger:pedido_humano"
    assert outcome.messages, "escalada sem mensagem nenhuma deixa a pessoa no vacuo"
    assert "equipe" in outcome.reply.lower()


async def test_recusa_de_orientacao_clinica_responde_sem_handoff(
    config: TenantConfig,
) -> None:
    provider = ScriptedProvider(script=[text_response("nao deveria ser chamado")])
    outcome = await make_engine(provider).run_turn(
        make_request(config, "qual remedio eu dou pra ele?")
    )

    assert provider.calls == []
    assert not outcome.escalate
    assert "veterinario" in outcome.reply.lower()


async def test_conversa_normal_chega_ao_modelo(config: TenantConfig) -> None:
    provider = ScriptedProvider(script=[text_response("Oi! Qual o nome do seu pet?")])
    outcome = await make_engine(provider).run_turn(make_request(config, "oi, queria marcar"))

    assert len(provider.calls) == 1
    assert outcome.reply == "Oi! Qual o nome do seu pet?"


# ============================== passo 8: saida ==============================


async def test_horario_vindo_de_tool_passa_sem_regenerar(config: TenantConfig) -> None:
    provider = ScriptedProvider(
        script=[
            tool_response("check_availability", {"date_from": "2026-09-09"}),
            text_response("Consigo na quarta, 9 de setembro, às 14h. Pode ser?"),
        ]
    )
    engine = make_engine(provider, availability_tool("2026-09-09T14:00:00-03:00"))

    outcome = await engine.run_turn(make_request(config, "queria marcar uma consulta"))

    assert not outcome.regenerated
    assert outcome.violations == ()
    assert "14h" in outcome.reply


async def test_horario_que_nao_veio_de_tool_e_descartado_e_regenerado(
    config: TenantConfig,
) -> None:
    """O criterio de aceite da S09, ponta a ponta.

    A primeira resposta promete um horario sem nunca ter chamado `check_availability`.
    Ela nao pode sair — e a pessoa tambem nao pode ficar sem resposta.
    """
    provider = ScriptedProvider(
        script=[
            text_response("Tenho quarta às 14h, pode ser?"),
            text_response("Deixa eu confirmar a agenda e ja te falo."),
        ]
    )
    outcome = await make_engine(provider).run_turn(make_request(config, "tem horario essa semana?"))

    assert len(provider.calls) == 2
    assert outcome.regenerated
    assert "hora_sem_evidencia" in outcome.violations
    assert outcome.reply == "Deixa eu confirmar a agenda e ja te falo."
    assert not outcome.escalate


async def test_a_regeneracao_pode_chamar_a_tool_que_faltava(config: TenantConfig) -> None:
    """A reescrita roda com tools: o caminho certo e o modelo buscar o horario de verdade."""
    provider = ScriptedProvider(
        script=[
            text_response("Tenho quarta às 14h!"),
            tool_response("check_availability", {"date_from": "2026-09-09"}),
            text_response("Confirmado: quarta, 9 de setembro, às 14h."),
        ]
    )
    engine = make_engine(provider, availability_tool("2026-09-09T14:00:00-03:00"))

    outcome = await engine.run_turn(make_request(config, "tem horario na quarta?"))

    assert outcome.regenerated
    assert not outcome.escalate
    assert outcome.used_tools == ("check_availability",)
    assert "14h" in outcome.reply


async def test_segunda_falha_escala_e_o_texto_do_modelo_nao_sai(
    config: TenantConfig,
) -> None:
    """Nao ha terceira tentativa: um humano assume e o que o modelo escreveu fica fora."""
    provider = ScriptedProvider(
        script=[
            text_response("Tenho quarta às 14h!"),
            text_response("Entao fica sexta às 9h, combinado!"),
        ]
    )
    outcome = await make_engine(provider).run_turn(make_request(config, "tem horario?"))

    assert outcome.escalate
    assert outcome.escalation_reason == "output_guardrail"
    assert outcome.stage == "handoff"
    assert "14h" not in outcome.reply
    assert "9h" not in outcome.reply
    assert outcome.reply == config.messages.out_of_scope.strip()


async def test_custo_da_regeneracao_entra_na_conta_do_turno(config: TenantConfig) -> None:
    """Se a reescrita saisse de graca, o disjuntor de custo da 11.5 dormiria."""
    provider = ScriptedProvider(
        script=[
            text_response("Tenho quarta às 14h!"),
            text_response("Vou confirmar a agenda e te falo."),
        ]
    )
    outcome = await make_engine(provider).run_turn(make_request(config, "tem horario?"))

    # Duas chamadas ao modelo, duas vezes o custo de uma.
    assert outcome.cost_usd == Decimal("0.008")
    assert outcome.usage.output_tokens == 400


async def test_preco_inventado_e_descartado(config: TenantConfig) -> None:
    provider = ScriptedProvider(
        script=[
            text_response("A consulta custa R$ 250,00."),
            text_response("Vou confirmar o valor com a equipe."),
        ]
    )
    outcome = await make_engine(provider).run_turn(make_request(config, "quanto custa a consulta?"))

    assert outcome.regenerated
    assert "preco_sem_evidencia" in outcome.violations


async def test_orientacao_clinica_e_substituida_sem_gastar_outra_chamada(
    config: TenantConfig,
) -> None:
    """Regenerar nao adianta: o modelo ja decidiu opinar, e a 11.5 manda substituir."""
    provider = ScriptedProvider(
        script=[text_response("Provavelmente e uma infeccao. Nao precisa se preocupar.")]
    )
    outcome = await make_engine(provider).run_turn(make_request(config, "ele esta com o olho ruim"))

    assert len(provider.calls) == 1
    assert "escopo_clinico" in outcome.violations
    assert "infeccao" not in outcome.reply.lower()


async def test_resposta_longa_sai_quebrada_em_varias_mensagens(config: TenantConfig) -> None:
    longa = " ".join(["Tudo certo por aqui, seguimos no horario combinado."] * 20)
    provider = ScriptedProvider(script=[text_response(longa)])

    outcome = await make_engine(provider).run_turn(make_request(config, "tudo bem?"))

    assert len(outcome.messages) > 1
    assert all(len(parte) <= config.persona.max_message_chars for parte in outcome.messages)
    assert outcome.reply == "\n\n".join(outcome.messages)


async def test_motor_sem_guardrail_nao_confere_nada(config: TenantConfig) -> None:
    """O padrao do motor continua inerte — mas producao instala os dois (`workers/base`)."""
    provider = ScriptedProvider(script=[text_response("Tenho quarta às 14h!")])
    engine = AgentEngine(provider=provider, audit=NullAuditSink(), extract=False)

    outcome = await engine.run_turn(make_request(config, "tem horario?"))

    assert outcome.reply == "Tenho quarta às 14h!"
    assert outcome.messages == ("Tenho quarta às 14h!",)
    assert not outcome.regenerated


# ================================= disjuntor =================================


async def test_disjuntor_continua_antes_de_tudo(config: TenantConfig) -> None:
    """Conversa cara demais nem chega ao guardrail de entrada."""
    provider = ScriptedProvider(script=[text_response("nao deveria ser chamado")])
    request = TurnRequest(
        tenant_id=TENANT_ID,
        conversation_id=CONVERSATION_ID,
        contact_id=CONTACT_ID,
        channel="whatsapp",
        config=config,
        now=NOW,
        state=ConversationState(
            history=(StoredMessage("inbound", "contact", "meu cachorro foi atropelado"),),
            cost_so_far_usd=Decimal("0.20"),
        ),
    )

    outcome = await make_engine(provider).run_turn(request)

    assert provider.calls == []
    assert outcome.escalation_reason == "max_llm_cost_usd_per_conversation"
