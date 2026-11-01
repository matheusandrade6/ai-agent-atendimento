"""Testes do proprio runner.

Uma suite de cenarios que aprova tudo e pior do que nenhuma: ela da o verde e cala o
alarme. Entao o que se prova aqui e o contrario do que os cenarios provam — que cada
assercao **reprova** quando a realidade nao bate, que chave desconhecida no YAML quebra
em vez de ser ignorada, e que o placar acusa cenario nao registrado e cenario
enfraquecido.

Os dois ultimos testes cuidam do criterio de aceite da S11: a suite roda offline
(nenhuma tool toca banco) e e deterministica (duas execucoes, o mesmo resultado).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.agent.tools.base import ToolContext, ToolResult
from app.agent.tools.cancel_appointment import cancel_appointment_tool
from app.agent.tools.find_appointments import find_appointments_tool
from app.agent.tools.reschedule_appointment import reschedule_appointment_tool
from app.calendar.resolver import no_calendars
from app.domain.tenant_config import load_tenant_config
from tests.conversational.fakes import FakeCalendar
from tests.conversational.harness import (
    TENANTS_DIR,
    Scenario,
    build_registry,
    load_scenario,
    run_scenario,
    scenario_paths,
)
from tests.conversational.placar import load_placar

BASE: dict[str, Any] = {
    "scenario": "meta",
    "services": [{"name": "Consulta clínica", "price_line": "R$ 180,00"}],
    "turns": [
        {
            "user": "quanto custa a consulta?",
            "agent": [
                {"call": "list_services", "arguments": {"query": "consulta"}},
                {"say": "A consulta clínica custa R$ 180,00."},
            ],
            "expect": {},
        }
    ],
}


def _com_expect(expect: dict[str, Any]) -> Scenario:
    data: dict[str, Any] = {
        **BASE,
        "turns": [{**BASE["turns"][0], "expect": expect}],
    }
    return Scenario.model_validate(data)


@pytest.mark.parametrize(
    ("expect", "trecho"),
    [
        ({"handoff_opened": True}, "handoff_opened"),
        ({"handoff_reason": "emergencia"}, "handoff_reason"),
        ({"response_contains_any": ["500"]}, "response_contains_any"),
        ({"response_excludes_any": ["180"]}, "response_excludes_any"),
        ({"no_tool_called": ["list_services"]}, "no_tool_called"),
        ({"tools_called": ["check_availability"]}, "tools_called"),
        ({"max_turns_to_escalate": 1}, "max_turns_to_escalate"),
        ({"stage": "booked"}, "stage"),
        ({"injection_flags": ["override"]}, "injection_flags"),
        ({"silenced": True}, "silenced"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
async def test_cada_assercao_reprova_quando_a_realidade_nao_bate(
    expect: dict[str, Any], trecho: str
) -> None:
    resultado = await run_scenario(_com_expect(expect))

    assert not resultado.passed
    assert any(trecho in falha for falha in resultado.failures), resultado.failures


async def test_assercoes_verdadeiras_aprovam() -> None:
    """O contrapeso do teste acima: o runner nao reprova por reprovar."""
    resultado = await run_scenario(
        _com_expect(
            {
                "handoff_opened": False,
                "response_contains_any": ["180"],
                "no_tool_called": ["check_availability"],
                "stage": "answering",
            }
        )
    )

    assert resultado.passed, resultado.report()


async def test_conteudo_do_cliente_vazado_no_system_prompt_reprova() -> None:
    """A assercao da invariante 7 tem de reprovar quando o texto nao vem delimitado.

    Um cenario sem chamada ao modelo (curto-circuito do guardrail) e o caso limite: nao
    ha prompt nenhum para conferir, e passar calado ali seria aprovar sem ter olhado.
    """
    scenario = Scenario.model_validate(
        {
            "scenario": "meta_isolamento",
            "turns": [
                {
                    "user": "socorro meu cachorro foi atropelado",
                    "expect": {"prompt_isolates_user_content": True},
                }
            ],
        }
    )

    resultado = await run_scenario(scenario)

    assert not resultado.passed
    assert "nao foi chamado" in resultado.failures[0]


def test_chave_desconhecida_no_cenario_e_recusada() -> None:
    """`respose_contains_any` nao pode virar assercao ignorada em silencio."""
    with pytest.raises(ValidationError):
        Scenario.model_validate(
            {
                "scenario": "meta",
                "turns": [{"user": "oi", "expect": {"respose_contains_any": ["x"]}}],
            }
        )


def test_passo_do_roteiro_precisa_de_say_ou_call() -> None:
    with pytest.raises(ValidationError):
        Scenario.model_validate(
            {
                "scenario": "meta",
                "turns": [{"user": "oi", "agent": [{"say": "oi", "call": "list_services"}]}],
            }
        )


def test_contagem_de_assercoes_soma_os_turnos() -> None:
    scenario = _com_expect({"handoff_opened": False, "response_contains_any": ["a", "b"]})

    #: Uma por campo escalar, uma por item de lista — e a lista que o placar protege.
    assert scenario.assertion_count() == 3


def test_placar_conhece_todos_os_cenarios_do_diretorio() -> None:
    placar = load_placar()

    assert {path.stem for path in scenario_paths()} == set(placar)


def test_placar_exige_motivo_em_falha_conhecida(tmp_path: Path) -> None:
    arquivo = tmp_path / "placar.yaml"
    arquivo.write_text(
        "versao: 1\natualizado_em: '2026-09-22'\n"
        "cenarios:\n  x:\n    status: known_failure\n    assercoes: 1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_placar(arquivo)


def test_calendario_falso_resolve_slot_relativo_e_respeita_ocupado() -> None:
    scenario = load_scenario(
        next(path for path in scenario_paths() if path.stem == "horario_inventado")
    )
    calendar = FakeCalendar(
        now=scenario.now.replace(tzinfo=None).astimezone(),
        timezone="America/Sao_Paulo",
        busy=("+1 16:00",),
    )

    livres = calendar.free_slots(["+1 14:30", "+1 16:00"])

    assert len(livres) == 1
    assert "14:30" in livres[0]["starts_at"]


def _stub_registry(tools: list[dict[str, Any]]) -> Any:
    scenario = Scenario.model_validate({"scenario": "meta_stub", "tools": tools, "turns": []})
    calendar = FakeCalendar(now=scenario.now.astimezone(), timezone="America/Sao_Paulo")
    return build_registry(scenario, calendar), calendar


async def _call(registry: Any, name: str, arguments: dict[str, Any]) -> ToolResult:
    ctx = ToolContext(
        tenant_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        channel="whatsapp",
        config=load_tenant_config(TENANTS_DIR / "clinica-exemplo.yaml"),
        now=datetime(2026, 9, 8, 10, 0, tzinfo=ZoneInfo("America/Sao_Paulo")),
    )
    result: ToolResult = await registry.get(name).handler(ctx, arguments)
    return result


async def test_stub_com_varias_respostas_entrega_uma_por_chamada_e_repete_a_ultima() -> None:
    """O cancelamento em dois turnos: a primeira chamada pergunta, a segunda cancela."""
    registry, _ = _stub_registry(
        [
            {
                "name": "cancel_appointment",
                "results": [
                    {"result": {"status": "confirmation_required"}},
                    {"result": {"status": "needs_human"}, "error": True},
                    {"result": {"status": "cancelled"}},
                ],
            }
        ]
    )

    answers = [await _call(registry, "cancel_appointment", {}) for _ in range(4)]

    assert [a.content["status"] for a in answers] == [
        "confirmation_required",
        "needs_human",
        "cancelled",
        "cancelled",  # esgotada a lista, a ultima se repete
    ]
    assert [a.is_error for a in answers] == [False, True, False, False]


async def test_instante_relativo_do_stub_vira_iso_com_offset_em_qualquer_profundidade() -> None:
    registry, calendar = _stub_registry(
        [
            {
                "name": "find_appointments",
                "result": {
                    "appointments": [{"starts_at": "@+1 14:30", "human": "texto"}],
                    "outro": ["@hoje 12:00", 3],
                },
            }
        ]
    )

    result = await _call(registry, "find_appointments", {})

    item = result.content["appointments"][0]
    assert item["starts_at"] == calendar.resolve("+1 14:30").isoformat()
    assert item["starts_at"].endswith("-03:00")  # offset: sem ele o guardrail ignora o instante
    assert result.content["outro"] == [calendar.resolve("hoje 12:00").isoformat(), 3]
    assert item["human"] == "texto"


@pytest.mark.parametrize(
    ("name", "factory", "kind"),
    [
        ("find_appointments", find_appointments_tool, "read"),
        ("reschedule_appointment", lambda: reschedule_appointment_tool(no_calendars), "write"),
        ("cancel_appointment", lambda: cancel_appointment_tool(no_calendars), "write"),
    ],
)
def test_stub_de_tool_de_producao_mantem_o_contrato_que_o_modelo_ve(
    name: str, factory: Any, kind: str
) -> None:
    """O cenario troca o handler, nao o que o modelo le: descricao, schema e validacao."""
    registry, _ = _stub_registry([{"name": name, "description": "descricao do cenario"}])
    production = factory()

    spec = registry.get(name)

    assert spec.description == production.description != "descricao do cenario"
    assert spec.input_schema == production.input_schema
    assert spec.args_model is production.args_model
    assert spec.kind == kind


def test_argumento_invalido_para_tool_de_producao_e_recusado_pela_validacao_real() -> None:
    """Cenario com `appointment_id` que nao e UUID quebra, em vez de passar por um stub frouxo.

    O motor valida os argumentos com `args_model` antes de chamar o handler; como o stub
    carrega o modelo de producao, o roteiro de um cenario nao consegue chamar a tool com
    argumento que a tool de verdade recusaria.
    """
    registry, _ = _stub_registry([{"name": "cancel_appointment"}])
    args_model = registry.get("cancel_appointment").args_model

    assert args_model is not None
    with pytest.raises(ValidationError):
        args_model.model_validate({"appointment_id": "abc"})
    args_model.model_validate({"appointment_id": str(uuid.uuid4())})


async def test_nenhum_cenario_toca_o_banco(monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterio de aceite da S11: a suite roda offline.

    As tools registradas pelo runner tem o handler trocado, mas o schema continua vindo
    das tools de producao — que importam `tenant_session`. Este teste falha no dia em
    que alguem registrar no runner uma tool que ainda consulta o banco.
    """

    def _proibido(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a suite conversacional nao pode abrir sessao de banco")

    monkeypatch.setattr("app.agent.tools.list_services.tenant_session", _proibido)
    monkeypatch.setattr("app.agent.tools.search_knowledge.search_knowledge", _proibido)
    monkeypatch.setattr("app.agent.tools.find_appointments.tenant_session", _proibido)
    monkeypatch.setattr("app.agent.tools.reschedule_appointment.tenant_session", _proibido)
    monkeypatch.setattr("app.agent.tools.cancel_appointment.tenant_session", _proibido)

    for path in scenario_paths():
        await run_scenario(load_scenario(path))


async def test_duas_execucoes_dao_o_mesmo_resultado() -> None:
    """Criterio de aceite da S11: deterministico nas assercoes nao-LLM."""
    for path in scenario_paths():
        scenario = load_scenario(path)
        primeira = await run_scenario(scenario)
        segunda = await run_scenario(scenario)

        assert primeira.transcript == segunda.transcript, path.stem
        assert primeira.failures == segunda.failures, path.stem
