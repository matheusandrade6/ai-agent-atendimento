"""Aritmetica de tempo (RNF-06).

Regra do projeto: nenhuma chamada a `datetime.now()` sem timezone em lugar nenhum.
Toda persistencia e UTC; toda regra de negocio e apresentacao usam a timezone da unidade.
O lint (`ruff` regra DTZ) recusa `datetime.now()` ingenuo; este modulo e a unica porta.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

__all__ = [
    "as_utc",
    "combine_local",
    "day_bounds_utc",
    "now_in",
    "now_utc",
    "to_tz",
    "tz_of",
]


def tz_of(timezone_name: str) -> ZoneInfo:
    """Resolve um nome IANA. Levanta se a timezone nao existir."""
    return ZoneInfo(timezone_name)


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_in(timezone_name: str) -> datetime:
    """Agora, na timezone da unidade. Ponto unico de entrada de 'agora'."""
    return datetime.now(tz_of(timezone_name))


def to_tz(moment: datetime, timezone_name: str) -> datetime:
    """Converte um instante com timezone para a timezone dada."""
    if moment.tzinfo is None:
        raise ValueError("datetime ingenuo: todo instante precisa de timezone")
    return moment.astimezone(tz_of(timezone_name))


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("datetime ingenuo: todo instante precisa de timezone")
    return moment.astimezone(UTC)


def combine_local(day: date, clock: time, timezone_name: str) -> datetime:
    """Junta data + hora local num instante com timezone.

    Em transicoes de horario de verao o `zoneinfo` resolve a ambiguidade pela regra
    padrao (fold=0). O motor de disponibilidade nao deve gerar slots dentro do salto:
    o teste de DST cobre esse caso explicitamente.
    """
    return datetime.combine(day, clock, tzinfo=tz_of(timezone_name))


def day_bounds_utc(day: date, timezone_name: str) -> tuple[datetime, datetime]:
    """Inicio e fim do dia local, expressos em UTC.

    Nao assume 24h: em dias de mudanca de horario de verao o intervalo tem 23h ou 25h.
    """
    tz = tz_of(timezone_name)
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    next_day_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return as_utc(start_local), as_utc(next_day_local)
