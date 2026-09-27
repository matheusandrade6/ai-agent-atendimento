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

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.conversational.fakes import FakeCalendar
from tests.conversational.harness import (
    Scenario,
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
