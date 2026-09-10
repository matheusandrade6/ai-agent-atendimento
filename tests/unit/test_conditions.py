"""Interpretador das condicoes de intake (`when` da secao 10.3).

Duas coisas sob teste: a gramatica aceita exatamente o que a spec usa, e nada fora dela
executa. A segunda importa mais — este e o unico ponto do sistema onde texto de arquivo
de config vira decisao.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.conditions import ConditionError, evaluate_condition, parse_condition


@pytest.mark.parametrize(
    ("expressao", "dados", "esperado"),
    [
        # exemplos literais da secao 10.3
        ("service.name contains 'vacina'", {"service": {"name": "Vacina V10"}}, True),
        ("service.name contains 'vacina'", {"service": {"name": "Consulta"}}, False),
        ("is_first_visit == false", {"is_first_visit": False}, True),
        ("is_first_visit == false", {"is_first_visit": True}, False),
        # o campo escalar tambem responde por `.name`
        ("service.name contains 'vacina'", {"service": "Vacina antirrabica"}, True),
        # acento e caixa nao mudam o resultado
        ("service.name contains 'vacina'", {"service": "VACINAÇÃO anual"}, True),
        # "sim"/"nao" do extrator contam como booleano
        ("is_first_visit == true", {"is_first_visit": "sim"}, True),
        ("is_first_visit == false", {"is_first_visit": "nao"}, True),
        # comparacoes numericas
        ("unanswered_questions >= 2", {"unanswered_questions": 2}, True),
        ("unanswered_questions >= 2", {"unanswered_questions": 1}, False),
        ("idade < 3", {"idade": "2"}, True),
        # negacao
        ("service not contains 'vacina'", {"service": "Consulta"}, True),
        ("species != gato", {"species": "cachorro"}, True),
        # listas
        ("services contains 'vacina'", {"services": ["consulta", "vacina V10"]}, True),
    ],
)
def test_avaliacao(expressao: str, dados: dict[str, Any], esperado: bool) -> None:
    assert evaluate_condition(expressao, dados) is esperado


def test_campo_ausente_torna_a_condicao_falsa() -> None:
    """Intake progressivo: a pergunta condicionada so aparece quando ha o que condicionar."""
    assert evaluate_condition("service.name contains 'vacina'", {}) is False
    assert evaluate_condition("is_first_visit == false", {}) is False


def test_caminho_que_nao_resolve_em_mapping_nao_estoura() -> None:
    assert evaluate_condition("service.detalhe.interno == 'x'", {"service": "Consulta"}) is False


@pytest.mark.parametrize(
    "expressao",
    [
        "__import__('os').system('rm -rf /')",
        "service.name == 'x' and 1 == 1",
        "service",
        "== 'x'",
        "service.name ~ 'vacina'",
        "1 + 1 == 2",
    ],
)
def test_expressao_fora_da_gramatica_e_recusada(expressao: str) -> None:
    """Nada que nao seja uma unica comparacao passa — em particular, nada executavel."""
    with pytest.raises(ConditionError):
        parse_condition(expressao)


def test_operador_longo_ganha_do_curto() -> None:
    """Sem a ordem certa, `>=` seria lido como `>` e o literal viraria `=2`."""
    condicao = parse_condition("n >= 2")
    assert condicao.operator == ">="
    assert condicao.literal == 2


def test_condicao_guarda_a_expressao_original() -> None:
    assert parse_condition(" is_first_visit == false ").source == "is_first_visit == false"


def test_config_com_when_invalido_falha_na_validacao() -> None:
    """O erro aparece no onboarding, nao na primeira conversa real."""
    from pydantic import ValidationError

    from app.domain.tenant_config import ConditionalIntake

    with pytest.raises(ValidationError):
        ConditionalIntake(when="isto nao e condicao", require=[])
