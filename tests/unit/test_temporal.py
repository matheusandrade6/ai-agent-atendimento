"""Gramatica de data e hora em portugues (secao 11.5).

Este arquivo e uma **lista de formas de escrever**, nao uma amostra. O motivo esta no
docstring de `app.agent.temporal`: forma nao reconhecida pela gramatica passa pelo
guardrail sem conferencia, e alucinacao de horario e o risco de maior impacto do projeto
(secao 21). Cada linha nova aqui e uma forma a menos de o agente prometer um horario que
nao existe.

A bateria esta dividida em tres:

1. **Reconhecer.** Trinta formas de escrever hora e dezesseis de escrever data.
2. **Nao reconhecer.** Duracao, dinheiro e artigo nao sao horario — e os falsos
   positivos custam handoff desnecessario.
3. **Emparelhar e conferir.** Data + hora viram um slot; slot so passa com prova.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.agent.temporal import (
    evidence_from_tool_result,
    find_mentions,
    fold,
    gather_evidence,
    pair_claims,
)

SAO_PAULO = "America/Sao_Paulo"
#: Segunda-feira. Escolhida de proposito: com "hoje" caindo numa segunda, "quarta" e
#: "quarta que vem" nao colidem por acidente.
TODAY = date(2026, 9, 7)


def times_in(text: str) -> set[time]:
    found: set[time] = set()
    for mention in find_mentions(text, today=TODAY):
        found |= mention.times
    return found


def dates_in(text: str) -> set[date]:
    found: set[date] = set()
    for mention in find_mentions(text, today=TODAY):
        found |= mention.dates
    return found


# ------------------------------ 1. reconhecer horario ------------------------------

#: Trinta formas de escrever um horario. O numero nao e enfeite: a suite conversacional
#: (S11) vai bater nisto, e cada forma que faltar aqui e uma brecha no guardrail.
HORARIOS: list[tuple[str, time]] = [
    ("14:00", time(14, 0)),
    ("14:00:00", time(14, 0)),
    ("14:30", time(14, 30)),
    ("19:45", time(19, 45)),
    ("14h", time(14, 0)),
    ("14 h", time(14, 0)),
    ("14hs", time(14, 0)),
    ("14hr", time(14, 0)),
    ("14hrs", time(14, 0)),
    ("14 horas", time(14, 0)),
    ("14h30", time(14, 30)),
    ("14h30min", time(14, 30)),
    ("14h30 min", time(14, 30)),
    ("14h 30", time(14, 30)),
    ("às 14", time(14, 0)),
    ("às 14h", time(14, 0)),
    ("às 9h", time(9, 0)),
    ("8h da manhã", time(8, 0)),
    ("10hs da manhã", time(10, 0)),
    ("8h30 da tarde", time(20, 30)),
    ("duas da tarde", time(14, 0)),
    ("2 da tarde", time(14, 0)),
    ("às 2 da tarde", time(14, 0)),
    ("oito da manhã", time(8, 0)),
    ("sete da noite", time(19, 0)),
    ("nove e meia da manhã", time(9, 30)),
    ("duas e meia da tarde", time(14, 30)),
    ("meio-dia", time(12, 0)),
    ("meio dia", time(12, 0)),
    ("meia-noite", time(0, 0)),
    ("meio-dia e meia", time(12, 30)),
    ("quinze para as três da tarde", time(14, 45)),
]


@pytest.mark.parametrize(("texto", "esperado"), HORARIOS, ids=[t for t, _ in HORARIOS])
def test_reconhece_o_horario_escrito(texto: str, esperado: time) -> None:
    assert esperado in times_in(f"Tenho {texto} disponivel.")


def test_a_bateria_de_horarios_cobre_pelo_menos_vinte_formas() -> None:
    """Guarda do proprio criterio de aceite da S09.

    Se alguem simplificar a lista acima, este teste cai junto — e a conversa sobre o
    corte acontece na revisao, nao em producao.
    """
    assert len({texto for texto, _ in HORARIOS}) >= 20


#: Sem periodo do dia, a hora escrita por extenso tem duas leituras. As duas entram:
#: descartar a certa custaria uma regeneracao e, na segunda, um handoff a toa.
AMBIGUOS: list[tuple[str, set[time]]] = [
    ("sete e quinze", {time(7, 15), time(19, 15)}),
    ("oito e meia", {time(8, 30), time(20, 30)}),
    ("9 e meia", {time(9, 30), time(21, 30)}),
    ("quinze para as três", {time(2, 45), time(14, 45)}),
]


@pytest.mark.parametrize(("texto", "esperado"), AMBIGUOS, ids=[t for t, _ in AMBIGUOS])
def test_hora_por_extenso_sem_periodo_tem_duas_leituras(texto: str, esperado: set[time]) -> None:
    assert times_in(f"Pode ser {texto}?") == esperado


def test_relogio_de_24h_nao_e_ambiguo() -> None:
    """`9h` e 09:00 e ponto. Aceitar 21:00 junto so alargaria o que o guardrail deixa passar."""
    assert times_in("às 9h") == {time(9, 0)}
    assert times_in("9:00") == {time(9, 0)}


# -------------------------------- 1b. reconhecer data --------------------------------

DATAS: list[tuple[str, date]] = [
    ("09/09", date(2026, 9, 9)),
    ("9/9", date(2026, 9, 9)),
    ("9/9/2026", date(2026, 9, 9)),
    ("09-09-2026", date(2026, 9, 9)),
    ("09/09/26", date(2026, 9, 9)),
    ("2026-09-09", date(2026, 9, 9)),
    ("9 de setembro", date(2026, 9, 9)),
    ("dia 9 de setembro", date(2026, 9, 9)),
    ("dia 9 de setembro de 2026", date(2026, 9, 9)),
    ("dia 9", date(2026, 9, 9)),
    ("hoje", date(2026, 9, 7)),
    ("amanhã", date(2026, 9, 8)),
    ("depois de amanhã", date(2026, 9, 9)),
    ("quarta", date(2026, 9, 9)),
    ("quarta-feira", date(2026, 9, 9)),
    ("quarta que vem", date(2026, 9, 9)),
    ("próxima quarta", date(2026, 9, 9)),
    ("sábado", date(2026, 9, 12)),
    ("domingo", date(2026, 9, 13)),
]


@pytest.mark.parametrize(("texto", "esperado"), DATAS, ids=[t for t, _ in DATAS])
def test_reconhece_a_data_escrita(texto: str, esperado: date) -> None:
    assert esperado in dates_in(f"Pode ser {texto}?")


def test_dia_da_semana_aceita_as_duas_leituras_de_que_vem() -> None:
    """ "Quarta que vem" nao tem leitura unica no Brasil; as duas semanas entram."""
    assert dates_in("quarta que vem") == {date(2026, 9, 9), date(2026, 9, 16)}


def test_data_sem_ano_considera_a_virada() -> None:
    assert dates_in("5 de janeiro") == {date(2026, 1, 5), date(2027, 1, 5)}


def test_data_impossivel_nao_vira_ocorrencia() -> None:
    assert dates_in("31 de fevereiro") == set()
    assert dates_in("31/02") == set()


# ------------------------------- 2. nao reconhecer -------------------------------

#: Cada linha aqui e um falso positivo que custaria uma resposta boa descartada.
NAO_SAO_HORARIO: list[str] = [
    "confirmamos 24 horas de antecedência",
    "cancelar com 2 horas antes",
    "a consulta dura 30 minutos",
    "o banho leva 2 horas",
    "te retorno em 2 horas",
    "daqui a 3 horas eu confirmo",
    "nas próximas 2 horas",
    "temos as 3 vagas ainda",
    "o valor é R$ 14,50",
    "atendemos as 4 especialidades",
]


@pytest.mark.parametrize("texto", NAO_SAO_HORARIO)
def test_duracao_e_artigo_nao_sao_horario(texto: str) -> None:
    assert times_in(texto) == set()


def test_crase_e_o_que_separa_hora_de_artigo() -> None:
    """`às 3` e hora; `as 3` e artigo. Dobrar o acento apagaria a diferenca."""
    assert times_in("às 3, tudo bem?") == {time(3, 0)}
    assert times_in("as 3 vagas, tudo bem?") == set()


# ---------------------------------- 3. emparelhar ----------------------------------


def test_data_e_hora_vizinhas_viram_um_slot() -> None:
    claims = pair_claims(find_mentions("Tenho quarta às 14h", today=TODAY))
    assert len(claims) == 1
    assert claims[0].kind == "slot"
    assert (date(2026, 9, 9), time(14, 0)) in claims[0].pairs


def test_data_e_hora_distantes_continuam_separadas() -> None:
    texto = "A quarta costuma ser mais tranquila por aqui, e o consultorio abre às 8h."
    claims = pair_claims(find_mentions(texto, today=TODAY))
    assert {claim.kind for claim in claims} == {"date", "time"}


def test_ocorrencia_mais_longa_ganha_da_mais_curta() -> None:
    mentions = find_mentions("dia 9 de setembro de 2026", today=TODAY)
    assert len(mentions) == 1
    assert mentions[0].dates == {date(2026, 9, 9)}


def test_fold_preserva_o_comprimento() -> None:
    """Os indices das ocorrencias dependem disto — `NFKD` deslocaria tudo."""
    original = "Sábado às 14h, na próxima terça-feira"
    assert len(fold(original)) == len(original)


# ----------------------------------- 4. evidencia -----------------------------------

SLOT_RESULT = {
    "slots": [
        {
            "slot_token": "eyJ...",
            "starts_at": "2026-09-09T14:00:00-03:00",
            "ends_at": "2026-09-09T14:30:00-03:00",
            "provider_name": "Dra. Ana",
            "human": "quarta-feira, 9 de setembro, às 14h",
        }
    ]
}


def test_instante_iso_do_resultado_vira_slot() -> None:
    evidence = evidence_from_tool_result(SLOT_RESULT, timezone=SAO_PAULO, today=TODAY)
    assert (date(2026, 9, 9), time(14, 0)) in evidence.slots
    assert evidence.allows_time(frozenset({time(14, 0)}))
    assert evidence.allows_date(frozenset({date(2026, 9, 9)}))


def test_instante_em_utc_vira_slot_na_timezone_da_unidade() -> None:
    """A tool pode devolver UTC; o cliente le o relogio da clinica."""
    evidence = evidence_from_tool_result(
        {"starts_at": "2026-09-09T17:00:00Z"}, timezone=SAO_PAULO, today=TODAY
    )
    assert (date(2026, 9, 9), time(14, 0)) in evidence.slots


def test_instante_sem_timezone_nao_vira_prova() -> None:
    """Sem offset nao da para saber se e local ou UTC — chutar seria inventar a prova."""
    evidence = evidence_from_tool_result(
        {"starts_at": "2026-09-09T14:00:00"}, timezone=SAO_PAULO, today=TODAY
    )
    assert evidence.slots == frozenset()


def test_texto_humano_do_resultado_tambem_conta() -> None:
    evidence = evidence_from_tool_result(
        {"human": "quinta-feira, 10 de setembro, às 16h"}, timezone=SAO_PAULO, today=TODAY
    )
    assert evidence.allows_time(frozenset({time(16, 0)}))
    assert evidence.allows_date(frozenset({date(2026, 9, 10)}))


def test_horario_de_funcionamento_entra_so_pelas_bordas() -> None:
    """O meio da janela nao prova disponibilidade: e onde mora a vaga inventada."""
    evidence = gather_evidence(
        business_hours=[(time(8, 0), time(19, 0))], timezone=SAO_PAULO, today=TODAY
    )
    assert evidence.allows_time(frozenset({time(8, 0)}))
    assert evidence.allows_time(frozenset({time(19, 0)}))
    assert not evidence.allows_time(frozenset({time(15, 0)}))


def test_data_real_com_hora_real_de_outro_slot_nao_passa() -> None:
    """A alucinacao mais dificil de ver lendo: dois pedacos verdadeiros, juntos errados."""
    evidence = gather_evidence(
        tool_results=[
            {"starts_at": "2026-09-09T14:00:00-03:00"},
            {"starts_at": "2026-09-10T16:00:00-03:00"},
        ],
        timezone=SAO_PAULO,
        today=TODAY,
    )
    assert evidence.allows_date(frozenset({date(2026, 9, 9)}))
    assert evidence.allows_time(frozenset({time(16, 0)}))
    assert not evidence.allows_slot(frozenset({(date(2026, 9, 9), time(16, 0))}))


def test_dst_nao_desloca_o_slot() -> None:
    """A conversao e por offset, nao por aritmetica ingenua de horas."""
    moment = datetime(2026, 2, 1, 18, 30, tzinfo=ZoneInfo("UTC"))
    evidence = evidence_from_tool_result(
        {"starts_at": moment.isoformat()}, timezone=SAO_PAULO, today=date(2026, 2, 1)
    )
    local = moment.astimezone(ZoneInfo(SAO_PAULO))
    assert (local.date(), local.time()) in evidence.slots
