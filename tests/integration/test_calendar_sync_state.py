"""`push_appointment_event` contra Postgres real (14.3, Sessao S12).

O teste unitario prova qual estado e escolhido; este prova que o `UPDATE` chega na linha
certa, atravessa a RLS com `app.tenant_id` e respeita o `CHECK` de `sync_status`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.calendar.base import CalendarSyncPending, EventDraft, ExternalEvent
from app.calendar.sync_state import push_appointment_event
from app.core.db import dispose_engine, tenant_session
from tests.unit.test_calendar_sync_state import FakeProvider

pytestmark = pytest.mark.integration

START = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
DRAFT = EventDraft(
    summary="Consulta", start=START, end=START + timedelta(hours=1), timezone="America/Sao_Paulo"
)


@pytest.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    await dispose_engine()
    yield
    await dispose_engine()


@pytest.fixture
def appointment(sync_engine: sa.Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """`(tenant_id, appointment_id)` com o agendamento em `synced` (o default)."""
    marker = uuid.uuid4().hex[:8]
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        tenant = conn.execute(
            sa.text(
                "INSERT INTO tenants (slug, name, vertical, config) "
                "VALUES (:s, :s, 'veterinaria', '{}'::jsonb) RETURNING id"
            ),
            {"s": f"s12-{marker}"},
        ).scalar_one()
        location = conn.execute(
            sa.text(
                "INSERT INTO locations (tenant_id, name, timezone, business_hours) "
                "VALUES (:t, 'Unidade', 'America/Sao_Paulo', '{}'::jsonb) RETURNING id"
            ),
            {"t": tenant},
        ).scalar_one()
        provider = conn.execute(
            sa.text(
                "INSERT INTO providers (tenant_id, location_id, name, working_hours) "
                "VALUES (:t, :l, 'Dra. Ana', '{}'::jsonb) RETURNING id"
            ),
            {"t": tenant, "l": location},
        ).scalar_one()
        service = conn.execute(
            sa.text(
                "INSERT INTO services (tenant_id, name, duration_minutes) "
                "VALUES (:t, 'Consulta', 30) RETURNING id"
            ),
            {"t": tenant},
        ).scalar_one()
        contact = conn.execute(
            sa.text(
                "INSERT INTO contacts (tenant_id, phone_e164, name) "
                "VALUES (:t, :p, 'Carla') RETURNING id"
            ),
            {"t": tenant, "p": f"+5511{marker}"},
        ).scalar_one()
        appt = conn.execute(
            sa.text(
                "INSERT INTO appointments "
                "(tenant_id, contact_id, provider_id, service_id, starts_at, ends_at) "
                "VALUES (:t, :c, :p, :s, :a, :b) RETURNING id"
            ),
            {
                "t": tenant,
                "c": contact,
                "p": provider,
                "s": service,
                "a": START,
                "b": START + timedelta(minutes=30),
            },
        ).scalar_one()
    yield tenant, appt
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :t"), {"t": tenant})


def _read(sync_engine: sa.Engine, tenant: uuid.UUID, appt: uuid.UUID) -> tuple[str, str | None]:
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(tenant)})
        row = conn.execute(
            sa.text("SELECT sync_status, external_event_id FROM appointments WHERE id = :a"),
            {"a": appt},
        ).one()
    return row[0], row[1]


async def test_sucesso_grava_o_id_do_evento_na_linha_do_agendamento(
    sync_engine: sa.Engine, appointment: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, appt = appointment
    provider = FakeProvider(ExternalEvent(id="ev-42", status="confirmed"))

    async with tenant_session(tenant) as session:
        await push_appointment_event(
            session, provider, appointment_id=appt, calendar_id="cal", draft=DRAFT
        )

    assert _read(sync_engine, tenant, appt) == ("synced", "ev-42")


async def test_falha_persistente_deixa_a_linha_em_pending_sync(
    sync_engine: sa.Engine, appointment: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, appt = appointment
    provider = FakeProvider(CalendarSyncPending("fora do ar", attempts=5))

    async with tenant_session(tenant) as session:
        await push_appointment_event(
            session, provider, appointment_id=appt, calendar_id="cal", draft=DRAFT
        )

    assert _read(sync_engine, tenant, appt) == ("pending_sync", None)
