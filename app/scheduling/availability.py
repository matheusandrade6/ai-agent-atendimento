"""Motor de disponibilidade (secao 13.2; RF-15, RF-16, RF-17, RNF-06).

Duas camadas, de proposito:

- `compute_slots` e o algoritmo da 13.2, puro: recebe jornadas, ocupacao e `now`, devolve
  slots. E o que a bateria da 19.1 exercita — horario de verao, virada de dia, jornada
  partida, feriado, buffer, recurso escasso, servico maior que a janela.
- `find_availability` busca as entradas (banco + calendario) e chama `compute_slots`.

O motor e a unica fonte de horario que o agente pode oferecer (invariante 1). Por isso ele
**falha fechado**: calendario que nao respondeu tira o profissional (ou o recurso) da
conta — nunca vira "livre". Quem ficou de fora volta em `AvailabilityResult.unavailable`,
para o agente poder dizer que nao conseguiu consultar em vez de dizer que nao ha vaga.

Ocupacao persistida
-------------------
`appointments.starts_at/ends_at` e `holds.starts_at/ends_at` guardam o horario do
**atendimento** (o slot oferecido), sem os buffers — e o que vai para o calendario e o que
a constraint `appt_no_overlap` compara. Ao ler a ocupacao do banco, o motor estende cada
linha pelos buffers do servico dela; assim o buffer de um atendimento ja marcado tambem
protege o proximo. (Contrato para a S14: gravar o slot, nao o bloco.)
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar.base import (
    CalendarError,
    CalendarProvider,
    CalendarQueryError,
    Interval,
)
from app.core.time import as_utc, day_bounds_utc, to_tz
from app.domain.models import (
    Appointment,
    Hold,
    Location,
    Provider,
    Resource,
    Service,
    ServiceProvider,
    ServiceResource,
)
from app.domain.tenant_config import TenantConfig
from app.scheduling.rules import (
    Preferences,
    SchedulingPolicy,
    ServiceDuration,
    WeeklySchedule,
    fits_capacity,
    is_free,
    merge_intervals,
    overlaps,
    uses_location,
    working_intervals,
)

__all__ = [
    "ACTIVE_APPOINTMENT_STATUSES",
    "AvailabilityRequest",
    "AvailabilityResult",
    "CalendarResolver",
    "ProviderAgenda",
    "ResourceAgenda",
    "Slot",
    "UnknownService",
    "compute_slots",
    "find_availability",
]

#: Os mesmos status que a constraint `appt_no_overlap` considera.
ACTIVE_APPOINTMENT_STATUSES: tuple[str, ...] = ("confirmed", "rescheduled")

#: Folga na busca de ocupacao: um atendimento que comeca na vespera, estendido pelos
#: buffers, ainda pode alcancar a primeira janela do dia.
_FETCH_MARGIN = timedelta(days=1)


# ------------------------------------- tipos -------------------------------------


@dataclass(frozen=True, slots=True)
class Slot:
    """Horario oferecivel. `start`/`end` em UTC; `timezone` e a da unidade, para exibir."""

    start: datetime
    end: datetime
    provider_id: uuid.UUID
    timezone: str
    resource_id: uuid.UUID | None = None

    @property
    def local_start(self) -> datetime:
        return to_tz(self.start, self.timezone)

    @property
    def local_end(self) -> datetime:
        return to_tz(self.end, self.timezone)

    def as_dict(self) -> dict[str, Any]:
        """Formato de saida de tool: hora local com offset, que e o que o agente le."""
        return {
            "start": self.local_start.isoformat(),
            "end": self.local_end.isoformat(),
            "provider_id": str(self.provider_id),
            "resource_id": str(self.resource_id) if self.resource_id else None,
        }


@dataclass(frozen=True, slots=True)
class ResourceAgenda:
    """Recurso escasso apto ao servico (`service_resources`).

    `bookings` sao usos que contam capacidade (agendamentos e holds com este recurso, ja
    com buffers). `blocked` e o calendario externo do recurso: o `freebusy` nao diz
    quantos lugares estao ocupados, entao intervalo ocupado la bloqueia o recurso inteiro.
    """

    resource_id: uuid.UUID
    capacity: int
    bookings: Sequence[Interval] = ()
    blocked: Sequence[Interval] = ()


@dataclass(frozen=True, slots=True)
class ProviderAgenda:
    """Tudo o que o algoritmo precisa saber de um profissional apto ao servico.

    `resources is None`: o servico nao exige recurso. Tupla vazia: exige, e nenhum esta
    disponivel (inclusive por calendario de recurso que nao respondeu) — logo, sem slot.
    """

    provider_id: uuid.UUID
    timezone: str
    business: WeeklySchedule
    working: WeeklySchedule
    duration: ServiceDuration
    busy: Sequence[Interval] = ()
    resources: Sequence[ResourceAgenda] | None = None


@dataclass(frozen=True, slots=True)
class AvailabilityRequest:
    """Entradas da 13.1. `date_from`/`date_to` sao dias locais da unidade, inclusivos."""

    service_id: uuid.UUID
    date_from: date
    date_to: date
    now: datetime
    provider_id: uuid.UUID | None = None
    preferences: Preferences = field(default_factory=Preferences)
    limit: int | None = None
    #: "Mais horarios" (RF-17): pula os `offset` primeiros da mesma ordenacao.
    offset: int = 0


@dataclass(frozen=True, slots=True)
class AvailabilityResult:
    slots: list[Slot]
    #: Profissional ou recurso que ficou fora da conta, com o motivo. Nao vazio significa
    #: "a lista pode estar incompleta" — nunca trate como "nao ha vaga".
    unavailable: Mapping[uuid.UUID, str] = field(default_factory=dict)


class UnknownService(LookupError):
    """Servico inexistente, inativo ou de outro tenant."""


#: Devolve o cliente de calendario que enxerga a agenda daquele profissional/recurso
#: (um por `credentials_ref`, D-42). `None` com `calendar_id` preenchido e erro de
#: configuracao — o dono fica de fora, nao livre.
CalendarResolver = Callable[[Provider | Resource], CalendarProvider | None]


# ----------------------------------- algoritmo -----------------------------------


def _days(first: date, last: date) -> Iterable[date]:
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


def _pick_resource(
    block: Interval, resources: Sequence[ResourceAgenda]
) -> tuple[bool, uuid.UUID | None]:
    for resource in resources:
        if any(overlaps(block, b) for b in resource.blocked):
            continue
        if fits_capacity(block, resource.bookings, resource.capacity):
            return True, resource.resource_id
    return False, None


def _provider_slots(
    agenda: ProviderAgenda,
    policy: SchedulingPolicy,
    *,
    date_from: date,
    date_to: date,
    now: datetime,
) -> list[Slot]:
    busy = merge_intervals(agenda.busy)
    busy_starts = [interval.start for interval in busy]
    duration = agenda.duration
    first = max(date_from, policy.first_day(now, agenda.timezone))
    last = min(date_to, policy.last_day(now, agenda.timezone))

    # Virada de dia: "22:00-24:00" de um dia e "00:00-02:00" do seguinte sao uma jornada
    # so. Janelas que se encostam viram uma, e o dia seguinte ao ultimo entra para que um
    # atendimento que comeca no ultimo dia possa terminar depois da meia-noite.
    windows = merge_intervals(
        window
        for day in _days(first, last + timedelta(days=1))
        for window in working_intervals(day, agenda.business, agenda.working, agenda.timezone)
    )

    slots: list[Slot] = []
    for window in windows:
        # Passo em tempo absoluto: num dia de horario de verao, os candidatos seguem o
        # relogio real e nenhum cai dentro da hora que nao existe.
        start = window.start
        while start + duration.core + duration.after <= window.end:
            block = duration.block_for(start)
            candidate = (
                block.start >= window.start
                and first <= to_tz(start, agenda.timezone).date() <= last
                and policy.accepts(start, now)
                and is_free(block, busy, busy_starts)
            )
            resource_id: uuid.UUID | None = None
            if candidate and agenda.resources is not None:
                candidate, resource_id = _pick_resource(block, agenda.resources)
            if candidate:
                slots.append(
                    Slot(
                        start=start,
                        end=start + duration.core,
                        provider_id=agenda.provider_id,
                        timezone=agenda.timezone,
                        resource_id=resource_id,
                    )
                )
            start += policy.granularity
    return slots


def compute_slots(
    agendas: Sequence[ProviderAgenda],
    policy: SchedulingPolicy,
    *,
    date_from: date,
    date_to: date,
    now: datetime,
    preferences: Preferences | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[Slot]:
    """Algoritmo da 13.2, passos 1 e 3-6, mais uniao, dedup, ordenacao e corte.

    A ordem de `agendas` desempata dois profissionais livres no mesmo horario.
    """
    if now.tzinfo is None:
        raise ValueError("`now` ingenuo: use app.core.time.now_in(tz)")
    preferences = preferences or Preferences()
    around = preferences.around or now

    def rank(slot: Slot) -> tuple[int, timedelta]:
        return (-preferences.adherence(slot.local_start), abs(slot.start - around))

    best: dict[datetime, tuple[tuple[int, timedelta, int], Slot]] = {}
    for order, agenda in enumerate(agendas):
        for slot in _provider_slots(agenda, policy, date_from=date_from, date_to=date_to, now=now):
            key = (*rank(slot), order)
            current = best.get(slot.start)
            if current is None or key < current[0]:
                best[slot.start] = (key, slot)

    ordered = [slot for _, slot in sorted(best.values(), key=lambda item: item[0])]
    cut = limit if limit is not None else policy.slots_per_offer
    return ordered[offset : offset + cut]


# ----------------------------------- entradas -----------------------------------


@dataclass(frozen=True, slots=True)
class _CalendarOwner:
    key: uuid.UUID
    calendar_id: str | None
    client: CalendarProvider | None


async def _fetch_busy(
    owners: Sequence[_CalendarOwner], start: datetime, end: datetime
) -> tuple[dict[uuid.UUID, list[Interval]], dict[uuid.UUID, str]]:
    """`freebusy` agrupado por cliente (13.2, passo 2a). Falha fecha so quem falhou."""
    busy: dict[uuid.UUID, list[Interval]] = {}
    failed: dict[uuid.UUID, str] = {}
    groups: dict[int, list[_CalendarOwner]] = defaultdict(list)
    clients: dict[int, CalendarProvider] = {}
    for owner in owners:
        if owner.calendar_id is None:
            busy[owner.key] = []  # agenda propria: so o banco conta
        elif owner.client is None:
            failed[owner.key] = "calendario sem credencial resolvida"
        else:
            groups[id(owner.client)].append(owner)
            clients[id(owner.client)] = owner.client

    for client_key, members in groups.items():
        calendar_ids = sorted({m.calendar_id for m in members if m.calendar_id})
        try:
            by_calendar = await clients[client_key].get_busy(calendar_ids, start, end)
            errors: dict[str, str] = {}
        except CalendarQueryError as exc:
            by_calendar, errors = exc.partial, exc.failed
        except CalendarError as exc:
            by_calendar, errors = {}, {cid: str(exc) or type(exc).__name__ for cid in calendar_ids}
        for member in members:
            assert member.calendar_id is not None
            if member.calendar_id in errors or member.calendar_id not in by_calendar:
                failed[member.key] = errors.get(member.calendar_id, "calendario sem resposta")
            else:
                busy[member.key] = list(by_calendar[member.calendar_id])
    return busy, failed


def _expanded(starts_at: datetime, ends_at: datetime, before: int, after: int) -> Interval:
    return Interval(
        as_utc(starts_at) - timedelta(minutes=before), as_utc(ends_at) + timedelta(minutes=after)
    )


async def _db_occupation(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    provider_ids: Sequence[uuid.UUID],
    resource_ids: Sequence[uuid.UUID],
    start: datetime,
    end: datetime,
    now: datetime,
) -> tuple[dict[uuid.UUID, list[Interval]], dict[uuid.UUID, list[Interval]]]:
    """Agendamentos ativos e holds vigentes (13.2, passos 2b e 2c), com buffers."""
    by_provider: dict[uuid.UUID, list[Interval]] = defaultdict(list)
    by_resource: dict[uuid.UUID, list[Interval]] = defaultdict(list)
    owner_filter_appt = [Appointment.provider_id.in_(provider_ids)]
    owner_filter_hold = [Hold.provider_id.in_(provider_ids)]
    if resource_ids:
        owner_filter_appt.append(Appointment.resource_id.in_(resource_ids))
        owner_filter_hold.append(Hold.resource_id.in_(resource_ids))

    appointments = (
        select(
            Appointment.provider_id,
            Appointment.resource_id,
            Appointment.starts_at,
            Appointment.ends_at,
            Service.buffer_before_min,
            Service.buffer_after_min,
        )
        .join(Service, Service.id == Appointment.service_id)
        .where(
            Appointment.tenant_id == tenant_id,
            Service.tenant_id == tenant_id,
            Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
            Appointment.starts_at < end,
            Appointment.ends_at > start,
            or_(*owner_filter_appt),
        )
    )
    holds = (
        select(
            Hold.provider_id,
            Hold.resource_id,
            Hold.starts_at,
            Hold.ends_at,
            Service.buffer_before_min,
            Service.buffer_after_min,
        )
        .join(Service, Service.id == Hold.service_id)
        .where(
            Hold.tenant_id == tenant_id,
            Service.tenant_id == tenant_id,
            Hold.status == "active",
            Hold.expires_at > now,
            Hold.starts_at < end,
            Hold.ends_at > start,
            or_(*owner_filter_hold),
        )
    )
    provider_set, resource_set = set(provider_ids), set(resource_ids)
    for statement in (appointments, holds):
        for provider_id, resource_id, starts_at, ends_at, before, after in (
            await session.execute(statement)
        ).all():
            interval = _expanded(starts_at, ends_at, before, after)
            if provider_id in provider_set:
                by_provider[provider_id].append(interval)
            if resource_id is not None and resource_id in resource_set:
                by_resource[resource_id].append(interval)
    return by_provider, by_resource


async def find_availability(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    config: TenantConfig,
    request: AvailabilityRequest,
    calendars: CalendarResolver,
) -> AvailabilityResult:
    """Busca as entradas da 13.2 e calcula os slots.

    `session` ja vem com `app.tenant_id` setado (`tenant_session`); mesmo assim toda query
    filtra `tenant_id` explicitamente (invariante 2).
    """
    if request.now.tzinfo is None:
        raise ValueError("`now` ingenuo: use app.core.time.now_in(tz)")
    service = await session.scalar(
        select(Service).where(
            Service.tenant_id == tenant_id,
            Service.id == request.service_id,
            Service.active.is_(True),
        )
    )
    if service is None:
        raise UnknownService(str(request.service_id))

    provider_query = (
        select(Provider, ServiceProvider.duration_override_min)
        .join(ServiceProvider, ServiceProvider.provider_id == Provider.id)
        .where(
            Provider.tenant_id == tenant_id,
            Provider.active.is_(True),
            ServiceProvider.service_id == service.id,
        )
        .order_by(Provider.name, Provider.id)
    )
    if request.provider_id is not None:
        provider_query = provider_query.where(Provider.id == request.provider_id)
    provider_rows = (await session.execute(provider_query)).all()
    if not provider_rows:
        return AvailabilityResult(slots=[])

    location_ids = {p.location_id for p, _ in provider_rows if p.location_id is not None}
    locations: dict[uuid.UUID, Location] = {}
    if location_ids:
        locations = {
            loc.id: loc
            for loc in await session.scalars(
                select(Location).where(
                    Location.tenant_id == tenant_id, Location.id.in_(location_ids)
                )
            )
        }

    resources: list[Resource] | None = None
    resource_links = select(ServiceResource.resource_id).where(
        ServiceResource.service_id == service.id
    )
    if (await session.scalars(resource_links)).first() is not None:
        resources = list(
            await session.scalars(
                select(Resource)
                .where(Resource.tenant_id == tenant_id, Resource.id.in_(resource_links))
                .order_by(Resource.name, Resource.id)
            )
        )

    tenant_tz = config.business_hours.timezone
    tenant_business = WeeklySchedule.from_business_hours(config.business_hours)

    def timezone_of(provider: Provider) -> str:
        location = locations.get(provider.location_id) if provider.location_id else None
        return location.timezone if location else tenant_tz

    def business_of(provider: Provider) -> WeeklySchedule:
        location = locations.get(provider.location_id) if provider.location_id else None
        if location and location.business_hours:
            return WeeklySchedule.from_json(location.business_hours)
        return tenant_business

    bounds = [
        bound
        for provider, _ in provider_rows
        for day in (request.date_from, request.date_to)
        for bound in day_bounds_utc(day, timezone_of(provider))
    ]
    fetch_start, fetch_end = min(bounds) - _FETCH_MARGIN, max(bounds) + _FETCH_MARGIN

    owners: list[_CalendarOwner] = [
        _CalendarOwner(p.id, p.calendar_id, calendars(p) if p.calendar_id else None)
        for p, _ in provider_rows
    ]
    owners += [
        _CalendarOwner(r.id, r.calendar_id, calendars(r) if r.calendar_id else None)
        for r in resources or ()
    ]
    calendar_busy, unavailable = await _fetch_busy(owners, fetch_start, fetch_end)

    provider_db, resource_db = await _db_occupation(
        session,
        tenant_id,
        provider_ids=[p.id for p, _ in provider_rows],
        resource_ids=[r.id for r in resources or ()],
        start=fetch_start,
        end=fetch_end,
        now=request.now,
    )

    in_person = uses_location(service.modality)
    agendas: list[ProviderAgenda] = []
    for provider, override in provider_rows:
        if provider.id in unavailable:
            continue
        provider_resources: list[ResourceAgenda] | None = None
        if resources is not None:
            provider_resources = [
                ResourceAgenda(
                    resource_id=r.id,
                    capacity=r.capacity,
                    bookings=resource_db.get(r.id, []),
                    blocked=calendar_busy[r.id],
                )
                for r in resources
                if r.id not in unavailable
                and (
                    not in_person
                    or r.location_id is None
                    or provider.location_id is None
                    or r.location_id == provider.location_id
                )
            ]
        agendas.append(
            ProviderAgenda(
                provider_id=provider.id,
                timezone=timezone_of(provider),
                business=business_of(provider),
                working=WeeklySchedule.from_json(provider.working_hours or {}),
                duration=ServiceDuration.resolve(
                    duration_minutes=service.duration_minutes,
                    buffer_before_min=service.buffer_before_min,
                    buffer_after_min=service.buffer_after_min,
                    override_minutes=override,
                ),
                busy=[*calendar_busy[provider.id], *provider_db.get(provider.id, [])],
                resources=provider_resources,
            )
        )

    slots = compute_slots(
        agendas,
        SchedulingPolicy.from_rules(config.scheduling),
        date_from=request.date_from,
        date_to=request.date_to,
        now=request.now,
        preferences=request.preferences,
        limit=request.limit,
        offset=request.offset,
    )
    return AvailabilityResult(slots=slots, unavailable=unavailable)
