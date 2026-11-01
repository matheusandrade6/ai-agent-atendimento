"""Massa e dublês compartilhados pelos testes de remarcar e cancelar (S16).

`test_tools_change.py` exercita as tools; `test_booking_change.py`, o domínio sem a tool.
Os dois precisam dos mesmos agendamentos, do mesmo calendário falso e do mesmo relógio.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Any

import sqlalchemy as sa

from app.agent.tools.base import ToolContext
from app.calendar.base import (
    CalendarProvider,
    EventDraft,
    EventPatch,
    ExternalEvent,
    Interval,
    SyncResult,
)
from app.core.time import combine_local
from app.domain.models import Provider, Resource
from app.domain.tenant_config import TenantConfig
from app.scheduling.availability import CalendarResolver
from tests.integration.booking_seed import BookingSeed

SP = "America/Sao_Paulo"
TUE = date(2026, 11, 10)
WED = date(2026, 11, 11)
NOW = combine_local(TUE, time(10, 0), SP)
#: O turno em que o cliente responde, minutos depois de a tool ter perguntado.
ANSWERED = NOW + timedelta(minutes=3)

#: `clinica-exemplo`: remarcar e cancelar exigem 4h de antecedencia.
MIN_NOTICE = timedelta(hours=4)
FAR = combine_local(WED, time(14, 0), SP)
NEAR = NOW + timedelta(hours=3)
EDGE = NOW + MIN_NOTICE

_ISO_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")


def iso_instants(content: Mapping[str, Any]) -> list[str]:
    """Os instantes ISO de um resultado de tool: cada um e prova de horario (D-55)."""
    return _ISO_INSTANT.findall(json.dumps(content))


def with_scheduling(config: TenantConfig, **changes: Any) -> TenantConfig:
    return config.model_copy(
        update={"scheduling": config.scheduling.model_copy(update=changes)},
    )


def ctx_for(
    seed: BookingSeed,
    config: TenantConfig,
    *,
    conversation: int = 0,
    now: datetime = NOW,
    contact: uuid.UUID | None = None,
) -> ToolContext:
    return ToolContext(
        tenant_id=seed.tenant,
        conversation_id=seed.conversations[conversation],
        contact_id=contact or seed.contact,
        channel="whatsapp",
        config=config,
        now=now,
    )


def rows(engine: sa.Engine, sql: str, **params: object) -> list[sa.Row[Any]]:
    with engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        return list(conn.execute(sa.text(sql), params).all())


def count(engine: sa.Engine, table: str, tenant: uuid.UUID) -> int:
    return int(rows(engine, f"SELECT count(*) FROM {table} WHERE tenant_id = :t", t=tenant)[0][0])


def active(engine: sa.Engine, seed: BookingSeed) -> int:
    sql = (
        "SELECT count(*) FROM appointments WHERE tenant_id = :t "
        "AND status IN ('confirmed','rescheduled')"
    )
    return int(rows(engine, sql, t=seed.tenant)[0][0])


def appt(engine: sa.Engine, appointment_id: uuid.UUID | str) -> sa.Row[Any]:
    return rows(
        engine,
        "SELECT status, cancellation, sync_status, external_event_id, intake, starts_at, "
        "provider_id, conversation_id FROM appointments WHERE id = :a",
        a=uuid.UUID(str(appointment_id)),
    )[0]


def insert_appointment(
    engine: sa.Engine,
    seed: BookingSeed,
    *,
    starts: datetime = FAR,
    provider: uuid.UUID | None = None,
    service: uuid.UUID | None = None,
    status: str = "confirmed",
    contact: uuid.UUID | None = None,
    event_id: str | None = None,
    sync_status: str = "synced",
    resource: uuid.UUID | None = None,
    minutes: int = 30,
) -> uuid.UUID:
    found = rows(
        engine,
        "INSERT INTO appointments (tenant_id, contact_id, provider_id, service_id, resource_id, "
        "starts_at, ends_at, status, external_event_id, sync_status, intake) VALUES (:t, :c, :p, "
        ":s, :r, :a, :b, :st, :ev, :sy, CAST(:intake AS jsonb)) RETURNING id",
        t=seed.tenant,
        c=contact or seed.contact,
        p=provider or seed.ana,
        s=service or seed.consulta,
        r=resource,
        a=starts,
        b=starts + timedelta(minutes=minutes),
        st=status,
        ev=event_id,
        sy=sync_status,
        intake=json.dumps({"subject_name": "Mia", "species": "gato"}),
    )
    value: uuid.UUID = found[0][0]
    return value


def another_contact(engine: sa.Engine, seed: BookingSeed) -> uuid.UUID:
    found = rows(
        engine,
        "INSERT INTO contacts (tenant_id, phone_e164, name) VALUES (:t, :p, 'Outra') RETURNING id",
        t=seed.tenant,
        p=f"+5511{uuid.uuid4().hex[:8]}",
    )
    value: uuid.UUID = found[0][0]
    return value


def give_calendar(engine: sa.Engine, provider: uuid.UUID, calendar_id: str) -> None:
    sql = (
        "UPDATE providers SET calendar_id = :c, calendar_provider = 'google', "
        "credentials_ref = 'cred-teste' WHERE id = :p RETURNING id"
    )
    assert rows(engine, sql, c=calendar_id, p=provider)


class FakeCalendar:
    """Calendario com estado: o que se afirma e sobre os eventos que sobraram."""

    def __init__(self) -> None:
        self.events: dict[tuple[str, str], EventDraft] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.create_error: Exception | None = None
        self.update_error: Exception | None = None
        self.delete_errors: dict[str, Exception] = {}
        #: Roda dentro do `update_event`, antes de ele responder — a janela entre o commit e a
        #: reversao, onde outro cliente pode ocupar o horario antigo.
        self.during_update: Callable[[], object] | None = None

    def put(self, calendar_id: str, event_id: str, start: datetime, minutes: int = 30) -> None:
        self.events[(calendar_id, event_id)] = EventDraft(
            summary="Consulta - Mia",
            start=start,
            end=start + timedelta(minutes=minutes),
            timezone=SP,
        )

    async def get_busy(
        self, calendar_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, list[Interval]]:
        return {calendar_id: [] for calendar_id in calendar_ids}

    async def create_event(
        self, calendar_id: str, event: EventDraft, idempotency_key: str
    ) -> ExternalEvent:
        self.calls.append(("create", calendar_id, idempotency_key))
        if self.create_error is not None:
            raise self.create_error
        event_id = f"evt-{idempotency_key[:8]}"
        self.events[(calendar_id, event_id)] = event
        return ExternalEvent(id=event_id, status="confirmed", start=event.start, end=event.end)

    async def update_event(
        self, calendar_id: str, event_id: str, patch: EventPatch
    ) -> ExternalEvent:
        self.calls.append(("update", calendar_id, event_id))
        if self.during_update is not None:
            self.during_update()
        if self.update_error is not None:
            raise self.update_error
        current = self.events[(calendar_id, event_id)]
        moved = replace(current, start=patch.start or current.start, end=patch.end or current.end)
        self.events[(calendar_id, event_id)] = moved
        return ExternalEvent(id=event_id, status="confirmed", start=moved.start, end=moved.end)

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.calls.append(("delete", calendar_id, event_id))
        if calendar_id in self.delete_errors:
            raise self.delete_errors[calendar_id]
        self.events.pop((calendar_id, event_id), None)

    async def sync_changes(self, calendar_id: str, sync_token: str | None) -> SyncResult:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def resolver_of(calendar: CalendarProvider | None) -> CalendarResolver:
    def resolve(owner: Provider | Resource) -> CalendarProvider | None:
        return calendar

    return resolve
