"""Expressoes condicionais da config de tenant (secoes 10.3 e 11.7).

O YAML do tenant carrega condicoes escritas por gente, nao por programador:

    when: "service.name contains 'vacina'"
    when: "is_first_visit == false"

Elas decidem se um campo de intake passa a ser obrigatorio. Quem decide isso e o
**codigo**, nunca o modelo (invariante 1 e secao 11.7) — por isso a avaliacao mora aqui,
num interpretador minusculo e fechado, e nao numa instrucao de prompt.

Por que nao `eval`
------------------
A string vem de arquivo versionado, mas um arquivo de config nao e codigo de aplicacao:
onboarding de cliente edita YAML. `eval` transformaria um erro de digitacao — ou um
colaborador mal-intencionado — em execucao arbitraria dentro do worker. A gramatica
abaixo aceita exatamente uma comparacao e nada mais.

Gramatica
---------
    condicao := caminho operador literal
    caminho  := ident ('.' ident)*
    operador := == | != | >= | <= | > | < | contains | not contains
    literal  := 'texto' | "texto" | numero | true | false | null

Semantica de campo ausente
--------------------------
Caminho que nao resolve faz a condicao ser **falsa**. Num intake progressivo isso e o
comportamento certo: enquanto a pessoa nao disser o servico, a pergunta condicionada ao
servico ainda nao existe. Assim que o campo e coletado, a condicao volta a ser avaliada
e a pergunta aparece.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = [
    "ConditionError",
    "evaluate_condition",
    "parse_condition",
]

Operator = Literal["==", "!=", ">=", "<=", ">", "<", "contains", "not contains"]

#: Ordem importa: o regex tenta os operadores mais longos primeiro, senao `>=` viraria
#: `>` seguido de um literal `=2`.
_OPERATORS: Final[tuple[str, ...]] = (
    "not contains",
    "contains",
    "==",
    "!=",
    ">=",
    "<=",
    ">",
    "<",
)

#: O literal e ancorado no fim da expressao e so aceita tres formas: string entre
#: aspas, ou um unico token sem espaco. E o que faz `x == 'a' and y == 'b'` ser
#: **recusado** em vez de virar uma comparacao contra o texto `'a' and y == 'b'` — erro
#: que passaria despercebido ate a pergunta condicional aparecer na hora errada.
_LITERAL: Final[str] = r"'[^']*'|\"[^\"]*\"|[^\s'\"]+"

_CONDITION_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<path>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
    r"\s*(?P<op>" + "|".join(re.escape(op) for op in _OPERATORS) + r")"
    r"\s*(?P<literal>" + _LITERAL + r")\s*$"
)

#: Ultimo segmento tolerado sobre um valor escalar. Deixa a condicao `service.name`
#: funcionar tanto com `service: "Vacina V10"` quanto com `service: {name: ...}` — os
#: dois formatos aparecem na pratica, dependendo de a tool ja ter resolvido o catalogo.
_SCALAR_LEAF: Final[frozenset[str]] = frozenset({"name", "label", "value", "text"})

#: Palavras que contam como "sim" quando o literal da condicao e booleano. A pessoa
#: responde "sim"; o extrator pode devolver texto em vez de bool.
_TRUE_WORDS: Final[frozenset[str]] = frozenset({"true", "1", "sim", "yes", "y", "s"})
_FALSE_WORDS: Final[frozenset[str]] = frozenset({"false", "0", "nao", "no", "n"})


class ConditionError(ValueError):
    """Expressao que nao cabe na gramatica. Levantada na validacao da config."""


_MISSING = object()


@dataclass(frozen=True, slots=True)
class Condition:
    """Uma comparacao ja parseada. Imutavel e barata de guardar em cache."""

    path: tuple[str, ...]
    operator: Operator
    literal: Any
    source: str

    def evaluate(self, data: Mapping[str, Any]) -> bool:
        value = _resolve(self.path, data)
        if value is _MISSING:
            return False
        return _compare(value, self.operator, self.literal)


def parse_condition(expression: str) -> Condition:
    """Traduz a expressao para uma `Condition`, ou levanta `ConditionError`.

    Chamada na validacao da `TenantConfig`: um `when` invalido derruba o onboarding,
    que e o momento barato de descobrir o erro — e nao a primeira conversa real.
    """
    match = _CONDITION_RE.match(expression)
    if match is None:
        raise ConditionError(
            f"condicao invalida: {expression!r}. "
            f"Formato aceito: `campo <operador> valor`, operadores {', '.join(_OPERATORS)}"
        )
    operator: Operator = match.group("op")  # type: ignore[assignment]
    return Condition(
        path=tuple(match.group("path").split(".")),
        operator=operator,
        literal=_parse_literal(match.group("literal")),
        source=expression.strip(),
    )


def evaluate_condition(expression: str, data: Mapping[str, Any]) -> bool:
    """Atalho para parsear e avaliar de uma vez."""
    return parse_condition(expression).evaluate(data)


# ------------------------------- resolucao -------------------------------


def _resolve(path: tuple[str, ...], data: Mapping[str, Any]) -> Any:
    current: Any = data
    for index, segment in enumerate(path):
        if isinstance(current, Mapping):
            if segment not in current:
                return _MISSING
            current = current[segment]
            continue
        # Escalar com um segmento sobrando: tolerado apenas para os nomes de _SCALAR_LEAF.
        is_last = index == len(path) - 1
        if is_last and segment in _SCALAR_LEAF and isinstance(current, str | int | float | bool):
            return current
        return _MISSING
    return current


def _parse_literal(raw: str) -> Any:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    # Literal sem aspas: tratado como texto. `service == consulta` funciona.
    return text


# ------------------------------- comparacao -------------------------------


def _compare(value: Any, operator: Operator, literal: Any) -> bool:
    if operator == "contains":
        return _contains(value, literal)
    if operator == "not contains":
        return not _contains(value, literal)
    if operator == "==":
        return _equals(value, literal)
    if operator == "!=":
        return not _equals(value, literal)
    return _ordered(value, operator, literal)


def _contains(value: Any, literal: Any) -> bool:
    needle = _fold(literal)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return any(_fold(item) == needle or needle in _fold(item) for item in value)
    return needle in _fold(value)


def _equals(value: Any, literal: Any) -> bool:
    if isinstance(literal, bool):
        coerced = _as_bool(value)
        return coerced is not None and coerced is literal
    if literal is None:
        return value is None
    if isinstance(literal, int | float) and not isinstance(literal, bool):
        left = _as_number(value)
        return left is not None and left == float(literal)
    return _fold(value) == _fold(literal)


def _ordered(value: Any, operator: Operator, literal: Any) -> bool:
    left = _as_number(value)
    right = _as_number(literal)
    if left is None or right is None:
        return False
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "<":
        return left < right
    return left <= right


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = _fold(value)
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
    return None


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def _fold(value: Any) -> str:
    """Normaliza para comparar: minusculas, sem acento, sem espaco nas pontas.

    "Vacinação" e "vacinacao" sao a mesma palavra para quem escreveu a condicao.
    """
    text = ("true" if value else "false") if isinstance(value, bool) else str(value)
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return without_accents.strip().lower()
