"""Placeholders de mensagem: quem sabe preencher, quem confere e a rede de seguranca.

Por que este modulo existe
--------------------------
Texto configurado pelo cliente cita campos entre chaves — `{address}`, `{phone}`,
`{date_human}`. Isso aparece hoje no `reply` de gatilho de escalonamento e em
`messages.confirmation`, e vai aparecer em toda mensagem nova que o onboarding escrever.
O defeito DEF-01 foi exatamente esse mecanismo faltando: o texto da emergencia saiu com
`{address}` e `{phone}` literais para quem estava com o animal atropelado no colo.

As tres camadas, na ordem em que agem
-------------------------------------
1. **Validacao.** `KNOWN_PLACEHOLDERS` e a lista do que o sistema sabe preencher. A
   config de tenant (`app.domain.tenant_config`) recusa texto que cite nome fora dela —
   um `{endereco}` escrito em portugues nunca teria como ser preenchido, e falhar no
   onboarding e de graca perto de falhar numa emergencia.
2. **Preenchimento.** `fill` e `str.format` sem `KeyError`: placeholder sem valor fica
   literal em vez de derrubar a mensagem inteira. A tolerancia e deliberada — um erro no
   caminho do gatilho de emergencia deixaria a pessoa **sem resposta nenhuma**, que e
   pior do que uma resposta imperfeita.
3. **Rede de seguranca.** `drop_unresolved` tira do texto o que sobrou sem valor. E a
   camada que impede o defeito de voltar no proximo template configurado: mesmo que
   alguem esqueca de passar os valores, o cliente nao le `{address}`. Ela roda em
   `app.agent.guardrails.split_message`, por onde passa toda mensagem que vai ao cliente.

Quem monta uma mensagem chama `fill`. Quem envia mensagem **sem** passar por
`split_message` (envio proativo por template do WhatsApp, por exemplo) chama
`drop_unresolved` antes de entregar o texto ao canal.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

__all__ = [
    "APPOINTMENT_PLACEHOLDERS",
    "CONTACT_PLACEHOLDERS",
    "KNOWN_PLACEHOLDERS",
    "PLACEHOLDER",
    "UNIT_PLACEHOLDERS",
    "drop_unresolved",
    "fill",
    "placeholders_in",
    "unknown_placeholders",
]

#: `{address}`, nao `{ADDRESS}` nem `{0}`: nome de identificador em minusculas. O recorte
#: estreito importa porque esta expressao tambem decide o que a rede de seguranca apaga —
#: `{VAR}` em maiusculas e `${VAR}` de ambiente ficam de fora de proposito.
PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

#: Campos da unidade, vindos da config de tenant (`TenantConfig.template_values`).
UNIT_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {"unit_name", "address", "phone", "agent_name"}
)

#: Quem esta do outro lado. Vem do contato, nao da config.
CONTACT_PLACEHOLDERS: Final[frozenset[str]] = frozenset({"contact_name"})

#: Campos de um agendamento. Quem preenche e o fluxo de confirmacao (S15/S16); estao
#: declarados aqui para `messages.confirmation` poder ser validada desde ja.
APPOINTMENT_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {"service", "subject_name", "provider", "date_human", "time", "price_line", "prep_line"}
)

#: Tudo que algum ponto do sistema sabe preencher. Nome novo em template exige codigo que
#: o preencha — por isso a lista mora aqui e nao no YAML do cliente.
KNOWN_PLACEHOLDERS: Final[frozenset[str]] = (
    UNIT_PLACEHOLDERS | CONTACT_PLACEHOLDERS | APPOINTMENT_PLACEHOLDERS
)

#: Uma frase: tudo ate o ponto (ou o fim da linha). A rede de seguranca descarta na
#: granularidade da frase, e nao do texto inteiro, para sobrar o que ainda e verdade.
_SENTENCE: Final[re.Pattern[str]] = re.compile(r"[^.!?\n]+[.!?]*")

_DOUBLE_SPACE: Final[re.Pattern[str]] = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCTUATION: Final[re.Pattern[str]] = re.compile(r"[ \t]+([.,;:!?])")
_DANGLING_SEPARATOR: Final[re.Pattern[str]] = re.compile(r"[ \t]*[:,;]+[ \t]*(?=[.!?]|\n|$)")
_BLANK_RUN: Final[re.Pattern[str]] = re.compile(r"\n{3,}")


def placeholders_in(text: str) -> tuple[str, ...]:
    """Nomes citados no texto, na ordem, sem repetir."""
    return tuple(dict.fromkeys(match.group(1) for match in PLACEHOLDER.finditer(text)))


def unknown_placeholders(text: str) -> tuple[str, ...]:
    """Nomes citados que nada no sistema sabe preencher."""
    return tuple(name for name in placeholders_in(text) if name not in KNOWN_PLACEHOLDERS)


def fill(template: str, values: Mapping[str, str]) -> str:
    """`str.format` que nao explode com placeholder desconhecido.

    O texto vem do YAML do cliente e pode citar `{address}` antes de existir um endereco
    para preencher. Um `KeyError` aqui deixaria a pessoa sem resposta num gatilho de
    emergencia — o lugar do projeto onde isso e menos aceitavel. Placeholder sem valor
    sai daqui literal e e a rede de seguranca (`drop_unresolved`) que o remove.
    """
    return PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def drop_unresolved(text: str) -> str:
    """Tira do texto o que ficou sem valor, sem nunca devolver mensagem vazia.

    Tres degraus, do melhor para o menos pior:

    1. **Frase fora.** A frase (ou a linha) que cita o placeholder e descartada inteira.
       "Va direto a clinica: {address}." desaparece e o resto da mensagem — "Vou chamar
       alguem da equipe agora." — continua valendo. Descartar a frase e melhor do que
       apagar so o campo, porque "va direto a clinica: ." promete um dado que nao veio.
    2. **So o campo.** Se nao sobrar nada do texto, o placeholder e apagado e o resto
       fica. Mensagem torta e ruim; silencio num gatilho de emergencia e pior.
    3. Texto sem placeholder volta intacto, sem custo nenhum.
    """
    if not PLACEHOLDER.search(text):
        return text

    kept: list[str] = []
    for line in text.splitlines():
        if not PLACEHOLDER.search(line):
            kept.append(line)
            continue
        survivors = [
            sentence.strip()
            for sentence in _SENTENCE.findall(line)
            if not PLACEHOLDER.search(sentence) and sentence.strip()
        ]
        if survivors:
            kept.append(" ".join(survivors))

    cleaned = _BLANK_RUN.sub("\n\n", "\n".join(kept)).strip()
    return cleaned or _tidy(PLACEHOLDER.sub("", text))


def _tidy(text: str) -> str:
    """Fecha os buracos que apagar um campo no meio da frase deixa para tras."""
    text = _DANGLING_SEPARATOR.sub("", text)
    text = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)
    text = _DOUBLE_SPACE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()
