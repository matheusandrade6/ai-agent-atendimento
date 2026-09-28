"""Placeholders de mensagem: preenchimento tolerante e rede de seguranca (DEF-01).

O que estes testes protegem e uma mensagem especifica: a do gatilho de emergencia, que
manda a pessoa sair de casa com o animal ferido e precisa levar endereco e telefone. Ela
saiu com `{address}` literal por onze sessoes.

A ordem dos testes segue as tres camadas: nome valido, preenchimento que nao explode,
limpeza do que sobrou.
"""

from __future__ import annotations

import pytest

from app.domain.templates import (
    KNOWN_PLACEHOLDERS,
    drop_unresolved,
    fill,
    placeholders_in,
    unknown_placeholders,
)

EMERGENCIA = (
    "Isso pode ser uma emergencia. Vou chamar alguem da equipe agora. "
    "Se estiver muito grave, va direto a clinica: {address}. Telefone: {phone}."
)


# ------------------------------- nomes citados -------------------------------


def test_placeholders_saem_na_ordem_e_sem_repetir() -> None:
    assert placeholders_in("{phone} e {address}, ou {phone}") == ("phone", "address")


@pytest.mark.parametrize(
    "texto",
    [
        "{Address}",  # maiuscula nao e placeholder
        "${WA_PHONE_NUMBER_ID}",  # variavel de ambiente (secao 18.1)
        "{0} {1}",  # posicional do str.format
        "{ address }",
        "cobramos {} por consulta",
    ],
)
def test_o_que_nao_e_placeholder_fica_de_fora(texto: str) -> None:
    """O recorte estreito importa: esta expressao decide o que a rede de seguranca apaga."""
    assert placeholders_in(texto) == ()


def test_nome_fora_da_lista_e_desconhecido() -> None:
    assert unknown_placeholders("va ate {endereco}") == ("endereco",)
    assert unknown_placeholders(EMERGENCIA) == ()


def test_a_lista_cobre_os_campos_da_confirmacao() -> None:
    """`messages.confirmation` da spec (10.3) e o template mais exigente que existe hoje."""
    confirmacao = (
        "{service} para {subject_name}, {date_human} as {time} com {provider} "
        "em {address}. {price_line} {prep_line}"
    )
    assert unknown_placeholders(confirmacao) == ()
    assert "contact_name" in KNOWN_PLACEHOLDERS


# ------------------------------- preenchimento -------------------------------


def test_preenche_o_que_tem_valor() -> None:
    texto = fill(EMERGENCIA, {"address": "Rua A, 10", "phone": "(11) 3333-4444"})
    assert "Rua A, 10" in texto
    assert "(11) 3333-4444" in texto
    assert placeholders_in(texto) == ()


def test_placeholder_sem_valor_nao_levanta() -> None:
    """A tolerancia e o ponto: `KeyError` aqui deixaria a emergencia sem resposta nenhuma."""
    texto = fill(EMERGENCIA, {"phone": "(11) 3333-4444"})
    assert "{address}" in texto
    assert "(11) 3333-4444" in texto


def test_valor_a_mais_e_ignorado() -> None:
    assert fill("oi {contact_name}", {"contact_name": "Ana", "provider": "Dra. Ana"}) == "oi Ana"


# ---------------------------- rede de seguranca ----------------------------


def test_texto_sem_placeholder_volta_intacto() -> None:
    limpo = "Vou chamar alguem da equipe agora."
    assert drop_unresolved(limpo) is limpo


def test_frase_com_placeholder_sai_e_o_resto_fica() -> None:
    saida = drop_unresolved(fill(EMERGENCIA, {}))
    assert "{" not in saida
    assert "Vou chamar alguem da equipe agora." in saida
    # A frase do endereco sai inteira: "va direto a clinica: ." prometeria um dado que
    # nao veio.
    assert "clinica" not in saida
    assert "Telefone" not in saida


def test_linha_do_template_de_confirmacao_sai_sozinha() -> None:
    """Cada campo da confirmacao esta na sua linha; falta de um nao pode levar as outras."""
    saida = drop_unresolved("Prontinho!\nConsulta para Rex\n{provider}\nAte amanha as 14h")
    assert saida == "Prontinho!\nConsulta para Rex\nAte amanha as 14h"


def test_placeholder_no_texto_todo_apaga_so_o_campo() -> None:
    """Degrau 2: mensagem torta e ruim, silencio num gatilho de emergencia e pior."""
    saida = drop_unresolved("Va direto a clinica: {address}. Telefone: {phone}.")
    assert saida == "Va direto a clinica. Telefone."


def test_texto_que_era_so_o_campo_fica_vazio() -> None:
    """Nao ha o que salvar de um texto sem nada alem do campo.

    Quem cobre este caso e `InputGuardrail.check`, que trata reply sem texto util como
    reply ausente e cai no aviso padrao — ver `test_guardrails.py`.
    """
    assert drop_unresolved("{address}") == ""
    assert drop_unresolved("Ligue para {phone}") == "Ligue para"
