"""Gramatica de data e hora em portugues do Brasil (secao 11.5, guardrail de saida).

Para que isto existe
--------------------
O guardrail de horario da 11.5 precisa responder uma pergunta muito concreta antes de
deixar uma resposta sair: **"o modelo acabou de afirmar um horario que nao veio de
tool?"**. Isso exige ler a resposta como gente le — `14h`, `14:00`, `as 14`,
`duas da tarde`, `quarta que vem`, `dia 9`, `amanha de manha` sao todos o mesmo tipo de
afirmacao, e todos precisam ser conferidos contra o resultado da tool.

Alucinacao de horario e o risco de maior impacto do projeto (secao 21). Quem defende
contra ele e este arquivo: **forma de escrever hora que nao for reconhecida aqui passa
sem conferencia**. Por isso a bateria de testes e uma lista de formas de escrever, nao
uma amostra.

Como o texto e lido
-------------------
1. O texto e *dobrado* (`fold`): minusculas e sem acento, **preservando o comprimento**.
   Preservar comprimento nao e detalhe: os indices das ocorrencias sao o que permite
   dizer que `quarta` e `14h` estao na mesma frase. `NFKD` deslocaria tudo.
2. Cada regra produz candidatos, nao um valor. `duas e meia` sem periodo do dia e 02:30
   ou 14:30; `quarta que vem` e a proxima quarta ou a seguinte. Resolver para um valor
   unico seria adivinhar; guardar o conjunto empurra a decisao para a conferencia.
3. Ocorrencias sobrepostas ficam com a mais longa (`09/09/2026` ganha de `09/09`).
4. Data e hora vizinhas viram **uma** afirmacao de slot (`pair_claims`). E essa juncao
   que pega o erro mais traicoeiro: uma data real e uma hora real que nunca estiveram
   no mesmo slot.

O que nao entra aqui
--------------------
Duracao nao e horario. `24 horas de antecedencia`, `2 horas antes`, `30 minutos` sao
recusados de proposito — um lembrete "24h antes" nao pode exigir prova de agenda.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Final

from app.core.time import to_tz

__all__ = [
    "Evidence",
    "TemporalClaim",
    "TemporalMention",
    "evidence_from_tool_result",
    "find_mentions",
    "fold",
    "gather_evidence",
    "iter_strings",
    "pair_claims",
]


# --------------------------------- normalizacao ---------------------------------

#: Tabela de um para um: dobra o acento sem mexer no comprimento do texto.
_ACCENTS: Final[dict[int, int]] = str.maketrans(
    "áàâãäéèêëíìîïóòôõöúùûüçÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇ",
    "aaaaaeeeeiiiiooooouuuucAAAAAEEEEIIIIOOOOOUUUUC",
)


def fold(text: str) -> str:
    """Minusculas e sem acento, com exatamente o mesmo comprimento do original."""
    return text.translate(_ACCENTS).lower()


# ----------------------------------- vocabulario -----------------------------------

_WRITTEN_HOUR: Final[Mapping[str, int]] = {
    "uma": 1,
    "duas": 2,
    "tres": 3,
    "quatro": 4,
    "cinco": 5,
    "seis": 6,
    "sete": 7,
    "oito": 8,
    "nove": 9,
    "dez": 10,
    "onze": 11,
    "doze": 12,
}

_WRITTEN_MINUTE: Final[Mapping[str, int]] = {
    "meia": 30,
    "um quarto": 15,
    "cinco": 5,
    "dez": 10,
    "quinze": 15,
    "vinte": 20,
    "vinte e cinco": 25,
    "trinta": 30,
    "quarenta": 40,
    "quarenta e cinco": 45,
    "cinquenta": 50,
}

_MONTHS: Final[Mapping[str, int]] = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}

#: `date.weekday()`: segunda = 0.
_WEEKDAYS: Final[Mapping[str, int]] = {
    "segunda": 0,
    "terca": 1,
    "quarta": 2,
    "quinta": 3,
    "sexta": 4,
    "sabado": 5,
    "domingo": 6,
}

_HOUR_WORDS: Final[str] = "|".join(_WRITTEN_HOUR)
_MINUTE_WORDS: Final[str] = "|".join(sorted(_WRITTEN_MINUTE, key=len, reverse=True))
_MONTH_WORDS: Final[str] = "|".join(_MONTHS)
_WEEKDAY_WORDS: Final[str] = "|".join(_WEEKDAYS)

#: O que transforma um numero em **duracao** em vez de horario, vindo *depois* dele:
#: `24 horas de antecedencia`, `2 horas antes`.
_DURATION_TAIL: Final[str] = (
    r"(?:de\s+anteced|antes\b|depois\b|de\s+duracao|de\s+atraso|de\s+intervalo|corridas?\b)"
)

#: O mesmo, vindo *antes*: `em 2 horas`, `daqui a 3 horas`, `nas proximas 2 horas`.
#: Sem isto, "te retorno em 2 horas" viraria uma promessa de agenda as 02:00 e o
#: guardrail descartaria uma resposta perfeitamente correta.
_DURATION_LEAD: Final[re.Pattern[str]] = re.compile(
    r"(?:\bem|\bdaqui\s+a|\bdentro\s+de|\bpor|\bdurante|\ba\s+cada|\bcada|\bleva|\bdura|"
    r"\bapos|\bdepois\s+de|\bultimas?|\bproximas?)\s*$"
)

#: `da tarde`, `de manha`, `pela manha`, `a tarde`.
_PERIOD: Final[str] = r"(?:d[ae]|pela|a)\s+(?P<period>manha|tarde|noite|madrugada)"


# ------------------------------------ ocorrencia ------------------------------------


@dataclass(frozen=True, slots=True)
class TemporalMention:
    """Um trecho do texto que afirma uma data **ou** uma hora.

    `dates` e `times` sao conjuntos de *candidatos*: a leitura pode ser ambigua e a
    ambiguidade morre na conferencia contra a evidencia, nao aqui.
    """

    rule: str
    raw: str
    start: int
    end: int
    dates: frozenset[date] = frozenset()
    times: frozenset[time] = frozenset()

    @property
    def kind(self) -> str:
        return "date" if self.dates else "time"


@dataclass(frozen=True, slots=True)
class TemporalClaim:
    """Afirmacao ja emparelhada: so data, so hora, ou o par (slot)."""

    raw: str
    dates: frozenset[date] = frozenset()
    times: frozenset[time] = frozenset()

    @property
    def kind(self) -> str:
        if self.dates and self.times:
            return "slot"
        return "date" if self.dates else "time"

    @property
    def pairs(self) -> frozenset[tuple[date, time]]:
        return frozenset((d, t) for d in self.dates for t in self.times)


@dataclass(frozen=True, slots=True)
class _Ctx:
    """O que uma regra precisa saber alem do proprio casamento."""

    today: date
    text: str


_Resolved = tuple[frozenset[date], frozenset[time]]
_Resolver = Callable[[re.Match[str], _Ctx], _Resolved]
_EMPTY: Final[_Resolved] = (frozenset(), frozenset())


# -------------------------------------- horas --------------------------------------


def _clock(hour: int, minute: int) -> frozenset[time]:
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return frozenset({time(hour, minute)})
    return frozenset()


def _with_period(hour: int, minute: int, period: str | None, *, ambiguous: bool) -> frozenset[time]:
    """Aplica o periodo do dia.

    `ambiguous` so vale para as formas que de fato nao dizem o periodo — `sete e
    quinze`, `quinze para as tres`. Ali as duas leituras entram. Ja `9h` e `as 9` sao
    lidas como 09:00: em portugues do Brasil o relogio de 24 horas e a convencao
    escrita, e aceitar 21:00 tambem so alargaria o que o guardrail deixa passar.
    """
    if not period:
        if ambiguous and 1 <= hour <= 11:
            return _clock(hour, minute) | _clock(hour + 12, minute)
        return _clock(hour, minute)
    if period == "noite" and hour == 12:
        return _clock(0, minute)
    if period in ("tarde", "noite"):
        return _clock(hour + 12 if hour < 12 else hour, minute)
    if period == "madrugada":
        return _clock(hour % 12, minute)
    return _clock(hour, minute)  # manha


def _rule_clock(match: re.Match[str], _ctx: _Ctx) -> _Resolved:
    """`14:00`, `9:05`, `14:00:00`."""
    return frozenset(), _clock(int(match["h"]), int(match["m"]))


def _rule_hour_marker(match: re.Match[str], ctx: _Ctx) -> _Resolved:
    """`14h`, `14 h`, `14hs`, `14hrs`, `14 horas`, `14h30`, `14h30min`, `8h da manha`."""
    before = fold(ctx.text[max(0, match.start() - 24) : match.start()])
    if _DURATION_LEAD.search(before):
        return _EMPTY
    minute = int(match["m"]) if match["m"] else 0
    return frozenset(), _with_period(int(match["h"]), minute, match["period"], ambiguous=False)


def _rule_bare_hour(match: re.Match[str], ctx: _Ctx) -> _Resolved:
    """`as 14`, `as 9 da manha` — hora sem marcador nenhum.

    Exige a crase no texto **original**: em portugues `as 3 vagas` e artigo e `as 3` e
    hora. Dobrar o acento apaga essa diferenca, entao a regra casa no texto dobrado e
    confere o acento no original. O agente escreve com acento; o cliente, nem sempre —
    e aqui quem esta sendo conferido e o agente.
    """
    if not ctx.text[match.start() : match.start() + 2].startswith("à"):
        return _EMPTY
    return frozenset(), _with_period(int(match["h"]), 0, match["period"], ambiguous=False)


def _rule_written_hour(match: re.Match[str], _ctx: _Ctx) -> _Resolved:
    """`duas da tarde`, `nove e meia da manha`, `sete e quinze`, `9 e meia`.

    Sem periodo do dia (`sete e quinze`) a hora escrita por extenso e mesmo ambigua —
    ninguem diz "dezenove e quinze" em conversa. As duas leituras entram.
    """
    raw_hour = match["h"]
    hour = int(raw_hour) if raw_hour.isdigit() else _WRITTEN_HOUR[raw_hour]
    raw_minute = match["m"]
    if not raw_minute:
        minute = 0
    elif raw_minute.isdigit():
        minute = int(raw_minute)
    else:
        minute = _WRITTEN_MINUTE[_squash(raw_minute)]
    period = match["period"]
    return frozenset(), _with_period(hour, minute, period, ambiguous=not period)


def _rule_quarter_to(match: re.Match[str], _ctx: _Ctx) -> _Resolved:
    """`quinze para as tres`, `um quarto para as duas da tarde`."""
    raw_hour = match["h"]
    hour = int(raw_hour) if raw_hour.isdigit() else _WRITTEN_HOUR[raw_hour]
    period = match["period"]
    return frozenset(), _with_period((hour - 1) % 24, 45, period, ambiguous=not period)


def _rule_noon(match: re.Match[str], _ctx: _Ctx) -> _Resolved:
    """`meio-dia`, `meio dia e meia`, `meia-noite`."""
    base = 12 if match["which"] == "dia" else 0
    return frozenset(), _clock(base, 30 if match["half"] else 0)


# -------------------------------------- datas --------------------------------------


def _safe_date(year: int, month: int, day: int) -> date | None:
    if not 1 <= month <= 12:
        return None
    if not 1 <= day <= calendar.monthrange(year, month)[1]:
        return None
    return date(year, month, day)


def _year_candidates(today: date, month: int, day: int) -> frozenset[date]:
    """Data sem ano: o ano corrente e o seguinte (a virada de ano e caso real)."""
    candidates = (_safe_date(today.year, month, day), _safe_date(today.year + 1, month, day))
    return frozenset(d for d in candidates if d is not None)


def _rule_iso(match: re.Match[str], _ctx: _Ctx) -> _Resolved:
    """`2026-09-09`."""
    day = _safe_date(int(match["y"]), int(match["mo"]), int(match["d"]))
    return (frozenset({day}) if day is not None else frozenset()), frozenset()


def _rule_numeric_date(match: re.Match[str], ctx: _Ctx) -> _Resolved:
    """`09/09`, `9/9/2026`, `09-09-26`."""
    day, month = int(match["d"]), int(match["mo"])
    raw_year = match["y"]
    if raw_year is None:
        return _year_candidates(ctx.today, month, day), frozenset()
    year = int(raw_year)
    if year < 100:
        year += 2000
    exact = _safe_date(year, month, day)
    return (frozenset({exact}) if exact is not None else frozenset()), frozenset()


def _rule_written_date(match: re.Match[str], ctx: _Ctx) -> _Resolved:
    """`9 de setembro`, `dia 9 de setembro de 2026`."""
    day = int(match["d"])
    month = _MONTHS[match["mo"]]
    raw_year = match["y"]
    if raw_year is None:
        return _year_candidates(ctx.today, month, day), frozenset()
    exact = _safe_date(int(raw_year), month, day)
    return (frozenset({exact}) if exact is not None else frozenset()), frozenset()


def _rule_day_only(match: re.Match[str], ctx: _Ctx) -> _Resolved:
    """`dia 9` — mes corrente e o seguinte."""
    day = int(match["d"])
    today = ctx.today
    rolls_over = today.month == 12
    next_month = 1 if rolls_over else today.month + 1
    next_year = today.year + 1 if rolls_over else today.year
    candidates = (_safe_date(today.year, today.month, day), _safe_date(next_year, next_month, day))
    return frozenset(d for d in candidates if d is not None), frozenset()


def _rule_relative_day(match: re.Match[str], ctx: _Ctx) -> _Resolved:
    """`hoje`, `amanha`, `depois de amanha`."""
    offset = {"hoje": 0, "amanha": 1, "depois de amanha": 2}[_squash(match["rel"])]
    return frozenset({ctx.today + timedelta(days=offset)}), frozenset()


def _rule_weekday(match: re.Match[str], ctx: _Ctx) -> _Resolved:
    """`quarta`, `quarta-feira`, `proxima quarta`, `quarta que vem`.

    "Quarta que vem" nao tem leitura unica no Brasil — para uns e a proxima quarta, para
    outros a da semana seguinte. As duas entram como candidatas: descartar a resposta
    certa por causa de um regionalismo custaria uma regeneracao e, na segunda, um
    handoff desnecessario.
    """
    today = ctx.today
    ahead = (_WEEKDAYS[match["wd"]] - today.weekday()) % 7
    explicit = bool(match["before"] or match["after"])
    first = today + timedelta(days=7 if (ahead == 0 and explicit) else ahead)
    return frozenset({first, first + timedelta(days=7)}), frozenset()


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


# -------------------------------------- regras --------------------------------------

#: Ordem = desempate quando duas regras casam o mesmo comprimento. Da mais especifica
#: para a mais geral.
_RULES: Final[tuple[tuple[str, re.Pattern[str], _Resolver], ...]] = (
    (
        "quarter_to",
        re.compile(
            r"\b(?:quinze|um\s+quarto)\s+para\s+as?\s+"
            rf"(?P<h>{_HOUR_WORDS}|\d{{1,2}})"
            rf"(?:\s+{_PERIOD})?"
        ),
        _rule_quarter_to,
    ),
    (
        "noon",
        re.compile(r"\bmei[oa][-\s]?(?P<which>dia|noite)(?:\s+e\s+(?P<half>meia))?\b"),
        _rule_noon,
    ),
    (
        "clock",
        re.compile(r"(?<![\d:])(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?::[0-5]\d)?(?![\d:])"),
        _rule_clock,
    ),
    (
        "hour_marker",
        re.compile(
            # `(?![a-z])` no lugar de `\b`: `14h30` nao tem fronteira entre o `h` e o
            # `3`, e era justamente a forma mais comum de escrever hora quebrada.
            r"(?<![\d,.])(?P<h>[01]?\d|2[0-3])\s*(?:h(?:s|r|rs|oras?)?|horas?)(?![a-z])"
            r"(?:\s*(?P<m>[0-5]\d)\s*(?:min\b|minutos?\b)?)?"
            rf"(?!\s*{_DURATION_TAIL})"
            # `\s*` e nao `\s+`: o grupo dos minutos ja pode ter comido o espaco, e sem
            # isso `8h30 da tarde` perderia o periodo e viraria 08:30.
            rf"(?:\s*{_PERIOD})?"
        ),
        _rule_hour_marker,
    ),
    (
        "written_hour",
        re.compile(
            rf"\b(?P<h>{_HOUR_WORDS}|\d{{1,2}})"
            rf"(?:\s+e\s+(?P<m>{_MINUTE_WORDS}|[0-5]\d))?"
            rf"\s+{_PERIOD}\b"
        ),
        _rule_written_hour,
    ),
    (
        "written_hour_bare",
        re.compile(
            rf"\b(?P<h>{_HOUR_WORDS}|\d{{1,2}})\s+e\s+(?P<m>{_MINUTE_WORDS})\b"
            r"(?!\s+(?:d[ae]|pela)\s)"
            r"(?P<period>)"
        ),
        _rule_written_hour,
    ),
    (
        "bare_hour",
        re.compile(
            r"\bas\s+(?P<h>[01]?\d|2[0-3])(?![\dh:])"
            rf"(?:\s+{_PERIOD})?"
            r"(?=\s*(?:[,.;!?)]|$|\s))"
        ),
        _rule_bare_hour,
    ),
    (
        "iso_date",
        re.compile(
            r"(?<!\d)(?P<y>20\d{2})-(?P<mo>0?[1-9]|1[0-2])-(?P<d>0?[1-9]|[12]\d|3[01])(?!\d)"
        ),
        _rule_iso,
    ),
    (
        "numeric_date",
        re.compile(
            r"(?<![\d/])(?P<d>0?[1-9]|[12]\d|3[01])[/-](?P<mo>0?[1-9]|1[0-2])"
            r"(?:[/-](?P<y>\d{4}|\d{2}))?(?![\d/])"
        ),
        _rule_numeric_date,
    ),
    (
        "written_date",
        re.compile(
            r"\b(?:dia\s+)?(?P<d>0?[1-9]|[12]\d|3[01])\s+de\s+"
            rf"(?P<mo>{_MONTH_WORDS})"
            r"(?:\s+de\s+(?P<y>20\d{2}))?\b"
        ),
        _rule_written_date,
    ),
    (
        "day_only",
        re.compile(r"\bdia\s+(?P<d>0?[1-9]|[12]\d|3[01])\b(?!\s*(?:de\s|[/-]|\d))"),
        _rule_day_only,
    ),
    (
        "relative_day",
        re.compile(r"\b(?P<rel>depois\s+de\s+amanha|amanha|hoje)\b"),
        _rule_relative_day,
    ),
    (
        "weekday",
        re.compile(
            r"(?:(?P<before>proxim[ao]|essa|esta|nest[ae])\s+)?"
            rf"\b(?P<wd>{_WEEKDAY_WORDS})(?:[-\s]?feira)?\b"
            r"(?:\s+(?P<after>que\s+vem|da\s+semana\s+que\s+vem))?"
        ),
        _rule_weekday,
    ),
)

_RULE_ORDER: Final[Mapping[str, int]] = {rule: index for index, (rule, _, _) in enumerate(_RULES)}


def find_mentions(text: str, *, today: date) -> tuple[TemporalMention, ...]:
    """Todas as afirmacoes de data e hora do texto, sem sobreposicao.

    Sobreposicao fica com a ocorrencia mais longa: `09/09/2026` ganha de `09/09`,
    `14h30` ganha de `14h`, `depois de amanha` ganha de `amanha`. Em empate de
    comprimento, a regra declarada primeiro ganha.
    """
    ctx = _Ctx(today=today, text=text)
    folded = fold(text)
    found: list[TemporalMention] = []
    for rule, pattern, resolve in _RULES:
        for match in pattern.finditer(folded):
            dates, times = resolve(match, ctx)
            if not dates and not times:
                continue
            found.append(
                TemporalMention(
                    rule=rule,
                    raw=text[match.start() : match.end()],
                    start=match.start(),
                    end=match.end(),
                    dates=dates,
                    times=times,
                )
            )

    kept: list[TemporalMention] = []
    for mention in sorted(found, key=lambda m: (-(m.end - m.start), _RULE_ORDER[m.rule], m.start)):
        if any(mention.start < other.end and other.start < mention.end for other in kept):
            continue
        kept.append(mention)
    return tuple(sorted(kept, key=lambda m: m.start))


#: Distancia maxima, em caracteres, entre data e hora para que sejam a mesma afirmacao.
#: `quarta as 14h` tem 1; um `14h` de outra frase, muito mais.
_PAIR_WINDOW: Final[int] = 24


def pair_claims(mentions: Sequence[TemporalMention]) -> tuple[TemporalClaim, ...]:
    """Junta data e hora vizinhas numa unica afirmacao de slot."""
    claims: list[TemporalClaim] = []
    index = 0
    while index < len(mentions):
        current = mentions[index]
        following = mentions[index + 1] if index + 1 < len(mentions) else None
        if (
            following is not None
            and current.kind != following.kind
            and following.start - current.end <= _PAIR_WINDOW
        ):
            claims.append(
                TemporalClaim(
                    raw=f"{current.raw} {following.raw}",
                    dates=current.dates | following.dates,
                    times=current.times | following.times,
                )
            )
            index += 2
            continue
        claims.append(TemporalClaim(raw=current.raw, dates=current.dates, times=current.times))
        index += 1
    return tuple(claims)


# ------------------------------------ evidencia ------------------------------------


@dataclass(frozen=True, slots=True)
class Evidence:
    """O que o turno provou existir. Tudo na timezone da unidade.

    Tres conjuntos, nao um. `slots` sao pares que sairam **juntos** de um resultado de
    tool — horario real de agenda. `dates` e `times` sao referencias soltas legitimas:
    a data que a tool devolveu, o horario de funcionamento, o que a base diz. Juntar os
    tres num conjunto so deixaria passar a combinacao de uma data real com uma hora
    real que nunca estiveram no mesmo slot — que e exatamente a alucinacao mais dificil
    de perceber lendo a resposta.
    """

    dates: frozenset[date] = frozenset()
    times: frozenset[time] = frozenset()
    slots: frozenset[tuple[date, time]] = frozenset()

    def __or__(self, other: Evidence) -> Evidence:
        return Evidence(
            dates=self.dates | other.dates,
            times=self.times | other.times,
            slots=self.slots | other.slots,
        )

    @property
    def is_empty(self) -> bool:
        return not (self.dates or self.times or self.slots)

    def allows_date(self, candidates: frozenset[date]) -> bool:
        return bool(candidates & (self.dates | {day for day, _ in self.slots}))

    def allows_time(self, candidates: frozenset[time]) -> bool:
        return bool(candidates & (self.times | {clock for _, clock in self.slots}))

    def allows_slot(self, pairs: frozenset[tuple[date, time]]) -> bool:
        return bool(pairs & self.slots)


def iter_strings(value: Any) -> Iterator[str]:
    """Todas as strings de uma estrutura JSON, em qualquer profundidade."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from iter_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        for item in value:
            yield from iter_strings(item)


_ISO_DATETIME: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)


def _parse_iso(raw: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def evidence_from_tool_result(content: Any, *, timezone: str, today: date) -> Evidence:
    """Extrai datas, horas e slots de um `tool_result`.

    Um instante ISO com offset (`2026-09-09T14:00:00-03:00`) e a prova mais forte que
    existe: vira **slot**, convertido para a timezone da unidade. O texto humano do
    mesmo resultado (`"quarta-feira, 9 de setembro, as 14h"`) e lido pela mesma
    gramatica da resposta do modelo — e o que permite conferir uma resposta escrita
    como gente contra um resultado escrito como gente.

    Instante sem timezone e ignorado de proposito: sem offset nao da para saber se
    `14:00` e local ou UTC, e chutar aqui seria inventar uma prova.
    """
    dates: set[date] = set()
    times: set[time] = set()
    slots: set[tuple[date, time]] = set()

    for raw in iter_strings(content):
        moments = [_parse_iso(found) for found in _ISO_DATETIME.findall(raw)]
        if moments:
            for moment in moments:
                if moment is None:
                    continue
                local = to_tz(moment, timezone)
                slots.add((local.date(), local.time().replace(second=0, microsecond=0)))
            continue
        for mention in find_mentions(raw, today=today):
            dates |= mention.dates
            times |= mention.times

    for day, clock in slots:
        dates.add(day)
        times.add(clock)
    return Evidence(dates=frozenset(dates), times=frozenset(times), slots=frozenset(slots))


def gather_evidence(
    *,
    tool_results: Iterable[Any] = (),
    texts: Iterable[str] = (),
    business_hours: Iterable[tuple[time, time]] = (),
    timezone: str,
    today: date,
) -> Evidence:
    """Junta tudo que o turno pode citar sem inventar.

    Tres fontes, em ordem de forca:

    1. **Resultado de tool.** A fonte que a 11.5 exige. Vira slot quando traz instante
       completo.
    2. **Texto da base de conhecimento e do catalogo.** "Atendemos das 8h as 19h" e
       resposta legitima e contem horario; sem esta fonte o agente ficaria proibido de
       dizer o proprio horario de funcionamento (docs/DECISOES.md, D-29).
    3. **Janelas de `business_hours`.** So as **bordas**, abertura e fechamento. O meio
       da janela nao prova nada: e exatamente ali que mora a vaga inventada.
    """
    evidence = Evidence()
    for content in tool_results:
        evidence = evidence | evidence_from_tool_result(content, timezone=timezone, today=today)

    dates: set[date] = set()
    times: set[time] = set()
    for text in texts:
        for mention in find_mentions(text, today=today):
            dates |= mention.dates
            times |= mention.times
    for opens, closes in business_hours:
        times.add(opens)
        times.add(closes)

    return evidence | Evidence(dates=frozenset(dates), times=frozenset(times))
