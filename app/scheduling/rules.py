"""Regras de negocio do agendamento (secao 13.3).

Tudo aqui e puro: sem banco, sem rede, sem relogio. Quem chama passa `now` e as janelas;
o motor (`app.scheduling.availability`) junta as pecas.

| Regra (13.3)              | Onde                                           |
|---------------------------|------------------------------------------------|
| Antecedencia minima       | `SchedulingPolicy.accepts`                     |
| Horizonte maximo          | `SchedulingPolicy.accepts` / `last_day`        |
| Buffers antes/depois      | `ServiceDuration.block_for`                    |
| Duracao por profissional  | `ServiceDuration.resolve(override_minutes=...)`|
| Almoco/intervalos         | lacunas em `WeeklySchedule` (`working_intervals`)|
| Feriados e excecoes       | `WeeklySchedule.exceptions`                    |
| Recurso escasso           | `fits_capacity`                                |
| Servico online            | `uses_location`                                |

Hora local e instante
---------------------
Janela de trabalho e regra de negocio, entao e escrita em hora local da unidade
("08:00"-"12:00"). O motor compara instantes: cada borda vira `datetime` em UTC aqui, uma
unica vez, por `local_to_utc`. Em dia de horario de verao a hora local pode nao existir
(salto) ou existir duas vezes (recuo); `local_to_utc` resolve as duas sem nunca dar a
janela uma hora que o relogio nao marcou — oferecer o que o profissional nao trabalha e
inventar disponibilidade (invariante 1).
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, Self

from app.calendar.base import Interval
from app.core.time import tz_of
from app.domain.tenant_config import (
    WEEKDAYS,
    BusinessHours,
    SchedulingRules,
    TimeWindow,
    Weekday,
)

__all__ = [
    "PERIOD_BOUNDS",
    "LocalWindow",
    "Period",
    "Preferences",
    "SchedulingPolicy",
    "ServiceDuration",
    "WeeklySchedule",
    "fits_capacity",
    "intersect_windows",
    "is_free",
    "local_to_utc",
    "merge_intervals",
    "overlaps",
    "uses_location",
    "weekday_of",
    "working_intervals",
]

_MINUTES_PER_DAY = 24 * 60

#: Janela local em minutos desde a meia-noite, `[inicio, fim)`. `fim == 1440` e "24:00":
#: trabalha ate a virada do dia. O schema (`TimeWindow`) nao aceita janela que atravessa a
#: meia-noite; quem trabalha 22h-02h declara 22:00-24:00 num dia e 00:00-02:00 no outro.
LocalWindow = tuple[int, int]

Period = Literal["morning", "afternoon", "evening"]

#: Faixas de `Preferences.periods`, em minutos locais. Mesma divisao que a pessoa usa ao
#: falar ("de manha", "a tarde", "a noite").
PERIOD_BOUNDS: Mapping[Period, LocalWindow] = {
    "morning": (0, 12 * 60),
    "afternoon": (12 * 60, 18 * 60),
    "evening": (18 * 60, _MINUTES_PER_DAY),
}


# ------------------------------- politica do tenant -------------------------------


@dataclass(frozen=True, slots=True)
class SchedulingPolicy:
    """O pedaco de `scheduling` (10.3) que o motor consome."""

    min_lead: timedelta
    max_horizon: timedelta
    granularity: timedelta
    slots_per_offer: int

    @classmethod
    def from_rules(cls, rules: SchedulingRules) -> Self:
        return cls(
            min_lead=timedelta(minutes=rules.min_lead_time_minutes),
            max_horizon=timedelta(days=rules.max_horizon_days),
            granularity=timedelta(minutes=rules.slot_granularity_minutes),
            slots_per_offer=rules.slots_per_offer,
        )

    def accepts(self, start: datetime, now: datetime) -> bool:
        """`now + min_lead <= start <= now + max_horizon` (13.2, passo 5)."""
        return now + self.min_lead <= start <= now + self.max_horizon

    def last_day(self, now: datetime, timezone_name: str) -> date:
        """Ultimo dia local que ainda pode ter slot. Limita a varredura, nao so o filtro."""
        return (now + self.max_horizon).astimezone(tz_of(timezone_name)).date()

    def first_day(self, now: datetime, timezone_name: str) -> date:
        return (now + self.min_lead).astimezone(tz_of(timezone_name)).date()


# ------------------------------------ duracao ------------------------------------


@dataclass(frozen=True, slots=True)
class ServiceDuration:
    """Duracao do atendimento e os buffers em volta (13.2, passo 3).

    O slot oferecido e o horario do **atendimento** — o que a pessoa le e o que vai para
    o calendario. Os buffers ocupam o profissional antes e depois, sem aparecer na oferta:
    `block_for` devolve o intervalo que precisa estar livre e dentro da janela.
    """

    before: timedelta
    core: timedelta
    after: timedelta

    @classmethod
    def resolve(
        cls,
        *,
        duration_minutes: int,
        buffer_before_min: int = 0,
        buffer_after_min: int = 0,
        override_minutes: int | None = None,
    ) -> Self:
        """`service_providers.duration_override_min` vence `services.duration_minutes`."""
        minutes = override_minutes if override_minutes is not None else duration_minutes
        if minutes <= 0:
            raise ValueError(f"duracao invalida: {minutes} min")
        if buffer_before_min < 0 or buffer_after_min < 0:
            raise ValueError("buffer negativo")
        return cls(
            before=timedelta(minutes=buffer_before_min),
            core=timedelta(minutes=minutes),
            after=timedelta(minutes=buffer_after_min),
        )

    @property
    def total(self) -> timedelta:
        return self.before + self.core + self.after

    def block_for(self, start: datetime) -> Interval:
        return Interval(start - self.before, start + self.core + self.after)


# ------------------------------ janelas de trabalho ------------------------------


def _parse_clock(raw: str, *, is_end: bool) -> int:
    hours, minutes = (int(part) for part in raw.split(":"))
    if is_end and hours == 24 and minutes == 0:
        return _MINUTES_PER_DAY
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"hora invalida: {raw!r}")
    return hours * 60 + minutes


def _windows(raw: Iterable[TimeWindow]) -> tuple[LocalWindow, ...]:
    parsed = [(_parse_clock(w.start, is_end=False), _parse_clock(w.end, is_end=True)) for w in raw]
    return tuple(sorted(parsed))


@dataclass(frozen=True, slots=True)
class WeeklySchedule:
    """Grade semanal + excecoes por data, em hora local.

    `weekly is None` significa "sem restricao semanal": e o `working_hours` vazio (`{}`) de
    um profissional que segue o horario da unidade. Com `weekly` presente, dia ausente e
    dia sem trabalho. Excecao substitui o dia inteiro; tupla vazia e fechado (feriado).
    """

    weekly: Mapping[Weekday, tuple[LocalWindow, ...]] | None
    exceptions: Mapping[date, tuple[LocalWindow, ...]] = field(default_factory=dict)

    @classmethod
    def from_business_hours(cls, hours: BusinessHours) -> Self:
        return cls(
            weekly={day: _windows(hours.weekly.get(day, [])) for day in WEEKDAYS},
            exceptions={
                date.fromisoformat(exc.date): () if exc.closed else _windows(exc.windows)
                for exc in hours.exceptions
            },
        )

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Self:
        """`providers.working_hours` / `locations.business_hours` (JSONB, formato da 10.4).

        Valida pelo mesmo schema do YAML; `timezone`, se vier, e ignorado aqui — a
        timezone e da unidade, nao da grade.
        """
        hours = BusinessHours.model_validate(dict(raw))
        schedule = cls.from_business_hours(hours)
        if "weekly" not in raw:
            return cls(weekly=None, exceptions=schedule.exceptions)
        return schedule

    def windows_on(self, day: date) -> tuple[LocalWindow, ...] | None:
        """Janelas do dia; `None` quando a grade nao restringe esse dia."""
        if day in self.exceptions:
            return self.exceptions[day]
        if self.weekly is None:
            return None
        return self.weekly.get(weekday_of(day), ())


def weekday_of(day: date) -> Weekday:
    return WEEKDAYS[day.weekday()]


def intersect_windows(
    first: Sequence[LocalWindow], second: Sequence[LocalWindow]
) -> tuple[LocalWindow, ...]:
    """Intersecao de duas listas ordenadas de janelas (merge linear)."""
    result: list[LocalWindow] = []
    i = j = 0
    while i < len(first) and j < len(second):
        start = max(first[i][0], second[j][0])
        end = min(first[i][1], second[j][1])
        if start < end:
            result.append((start, end))
        if first[i][1] < second[j][1]:
            i += 1
        else:
            j += 1
    return tuple(result)


def local_to_utc(day: date, minutes: int, timezone_name: str) -> datetime:
    """Hora local de uma borda de janela, como instante UTC.

    Regra unica: o **primeiro instante em que o relogio de parede marca aquela hora**.

    - Hora que existe duas vezes (recuo do horario de verao): a primeira ocorrencia.
    - Hora que nao existe (salto): o instante da transicao, quando o relogio pula por
      cima dela. "Abre as 02:30" num dia em que 02:00 vira 03:00 abre as 03:00; "fecha as
      02:30" fecha as 03:00 tambem — a janela nunca ganha a hora que nao existiu.
    """
    tz = tz_of(timezone_name)
    if minutes == _MINUTES_PER_DAY:
        naive = datetime.combine(day + timedelta(days=1), time.min)
    else:
        naive = datetime.combine(day, time(minutes // 60, minutes % 60))
    first, second = (naive.replace(tzinfo=tz, fold=fold).astimezone(UTC) for fold in (0, 1))
    if first <= second:
        return first
    # Salto: `second` (offset de depois) cai antes da transicao e `first` depois dela.
    # Transicoes acontecem em minuto cheio; o salto tem no maximo algumas dezenas deles.
    offset_before = second.astimezone(tz).utcoffset()
    moment = second
    while moment.astimezone(tz).utcoffset() == offset_before:
        moment += timedelta(minutes=1)
    return moment


def working_intervals(
    day: date,
    business: WeeklySchedule,
    working: WeeklySchedule,
    timezone_name: str,
) -> list[Interval]:
    """`working_hours ∩ business_hours - excecoes` de um dia local, em UTC (13.2, passo 1).

    A lacuna entre duas janelas (almoco, jornada partida) simplesmente nao vira intervalo.
    """
    business_windows = business.windows_on(day)
    working_windows = working.windows_on(day)
    if business_windows is None and working_windows is None:
        return []  # sem grade nenhuma nao ha jornada: nunca "aberto 24h" por omissao
    if business_windows is None:
        local = tuple(working_windows or ())
    elif working_windows is None:
        local = business_windows
    else:
        local = intersect_windows(business_windows, working_windows)

    intervals: list[Interval] = []
    for start_min, end_min in local:
        start = local_to_utc(day, start_min, timezone_name)
        end = local_to_utc(day, end_min, timezone_name)
        if start < end:
            intervals.append(Interval(start, end))
    return intervals


# ----------------------------------- ocupacao -----------------------------------


def overlaps(a: Interval, b: Interval) -> bool:
    """Intervalos semiabertos `[start, end)`: encostar nao e sobrepor."""
    return a.start < b.end and b.start < a.end


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Une intervalos que se sobrepoem ou se tocam, ordenados por inicio."""
    merged: list[Interval] = []
    for current in sorted(intervals, key=lambda iv: (iv.start, iv.end)):
        if merged and current.start <= merged[-1].end:
            last = merged[-1]
            if current.end > last.end:
                merged[-1] = Interval(last.start, current.end)
        else:
            merged.append(current)
    return merged


def is_free(block: Interval, merged_busy: Sequence[Interval], starts: Sequence[datetime]) -> bool:
    """`block` nao intersecta nenhum intervalo de `merged_busy` (ordenado e unido).

    `starts` e a lista de inicios de `merged_busy`, para a busca binaria.
    """
    index = bisect.bisect_left(starts, block.end)
    # So o intervalo imediatamente anterior ao fim do bloco pode alcanca-lo: unidos e
    # ordenados, os fins tambem crescem.
    return index == 0 or merged_busy[index - 1].end <= block.start


def fits_capacity(block: Interval, bookings: Iterable[Interval], capacity: int) -> bool:
    """Recurso com `capacity` lugares comporta mais um uso em `block`?

    Conta o pico de usos simultaneos **dentro do bloco**, nao o total de usos que o tocam:
    duas reservas em sequencia numa sala de capacidade 1 nao a lotam.
    """
    if capacity <= 0:
        return False
    events: list[tuple[datetime, int]] = []
    for booking in bookings:
        if overlaps(booking, block):
            events.append((max(booking.start, block.start), 1))
            events.append((min(booking.end, block.end), -1))
    # Fim antes de inicio no mesmo instante: quem sai libera o lugar para quem entra.
    events.sort(key=lambda event: (event[0], event[1]))
    peak = current = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak < capacity


# ---------------------------------- preferencias ----------------------------------


@dataclass(frozen=True, slots=True)
class Preferences:
    """Preferencias da pessoa (13.1). Ordenam a oferta; **nao** filtram.

    Preferencia impossivel nao zera a oferta: os slots mais aderentes vem primeiro e o
    agente explica que o pedido exato nao tem vaga (cenario "preferencia impossivel",
    19.3). `around` e o instante de referencia para "proximidade temporal"; sem ele, vale
    o `now` da consulta — o mais cedo primeiro.
    """

    periods: frozenset[Period] = frozenset()
    weekdays: frozenset[Weekday] = frozenset()
    not_before: time | None = None
    not_after: time | None = None
    around: datetime | None = None

    def adherence(self, local_start: datetime) -> int:
        """Quantas das preferencias declaradas o horario (local) atende."""
        minutes = local_start.hour * 60 + local_start.minute
        score = 0
        if self.periods and any(
            PERIOD_BOUNDS[p][0] <= minutes < PERIOD_BOUNDS[p][1] for p in self.periods
        ):
            score += 1
        if self.weekdays and weekday_of(local_start.date()) in self.weekdays:
            score += 1
        if self.not_before is not None and local_start.time() >= self.not_before:
            score += 1
        if self.not_after is not None and local_start.time() <= self.not_after:
            score += 1
        return score


# ----------------------------------- modalidade -----------------------------------


def uses_location(modality: str) -> bool:
    """Servico online ignora `location` (13.3): sem endereco no evento e sem exigir que o
    recurso seja da unidade do profissional. A jornada continua valendo."""
    return modality != "online"
