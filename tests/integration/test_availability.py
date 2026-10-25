"""`find_availability` contra Postgres real (13.2, passo 2; Sessao S13).

O algoritmo e coberto pela bateria unitaria (`tests/unit/test_scheduling_availability.py`).
Aqui se prova o que so o banco mostra: quais linhas viram ocupacao (agendamento ativo, hold
vigente — com os buffers do servico de cada um), quais nao viram (cancelado, hold vencido),
de onde vem a jornada (`locations.business_hours`), o recurso disputado entre
profissionais, e que calendario sem resposta tira o profissional da conta.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from app.calendar.base import CalendarProvider, CalendarSyncPending, Interval
from app.core.db import dispose_engine, tenant_session
from app.core.time import combine_local
from app.domain.models import Provider, Resource
from app.domain.tenant_config import TenantConfig, load_tenant_config
from app.scheduling.availability import (
    AvailabilityRequest,
    AvailabilityResult,
    UnknownService,
    find_availability,
)
from tests.conftest import TENANTS_DIR

pytestmark = pytest.mark.integration

SP = "America/Sao_Paulo"
TUE = date(2026, 9, 8)
WED = date(2026, 9, 9)
NOW = combine_local(TUE, time(10, 0), SP)


def at(hour: int, minute: int = 0, day: date = WED) -> datetime:
    return combine_local(day, time(hour, minute), SP)


class FakeCalendar:
    def __init__(self, busy: dict[str, list[Interval]] | Exception) -> None:
        self.busy = busy

    async def get_busy(
        self, calendar_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, list[Interval]]:
        if isinstance(self.busy, Exception):
            raise self.busy
        return {cid: self.busy.get(cid, []) for cid in calendar_ids}

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


@dataclass
class Seed:
    tenant: uuid.UUID
    ana: uuid.UUID
    bruno: uuid.UUID
    room: uuid.UUID
    consulta: uuid.UUID
    banho: uuid.UUID
    cirurgia: uuid.UUID


@pytest.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    await dispose_engine()
    yield
    await dispose_engine()


@pytest.fixture
def config() -> TenantConfig:
    return load_tenant_config(TENANTS_DIR / "clinica-exemplo.yaml")


@pytest.fixture
def seed(sync_engine: sa.Engine) -> Iterator[Seed]:
    marker = uuid.uuid4().hex[:8]
    # A unidade so abre de manha na quarta: prova que `locations.business_hours` vence o
    # horario do YAML do tenant.
    business = {"weekly": {"wed": [{"start": "08:00", "end": "12:00"}]}}

    def one(conn: sa.Connection, sql: str, **params: Any) -> uuid.UUID:
        value: uuid.UUID = conn.execute(sa.text(sql), params).scalar_one()
        return value

    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        t = one(
            conn,
            "INSERT INTO tenants (slug, name, vertical, config) "
            "VALUES (:s, :s, 'veterinaria', '{}'::jsonb) RETURNING id",
            s=f"s13-{marker}",
        )
        loc = one(
            conn,
            "INSERT INTO locations (tenant_id, name, timezone, business_hours) "
            "VALUES (:t, 'Unidade', :tz, CAST(:bh AS jsonb)) RETURNING id",
            t=t,
            tz=SP,
            bh=json.dumps(business),
        )
        provider_sql = (
            "INSERT INTO providers (tenant_id, location_id, name, working_hours, calendar_id) "
            "VALUES (:t, :l, :n, '{}'::jsonb, :c) RETURNING id"
        )
        ana = one(conn, provider_sql, t=t, l=loc, n="Ana", c="ana@cal")
        bruno = one(conn, provider_sql, t=t, l=loc, n="Bruno", c=None)
        service_sql = (
            "INSERT INTO services (tenant_id, name, duration_minutes, buffer_before_min, "
            "buffer_after_min) VALUES (:t, :n, :d, :b, :a) RETURNING id"
        )
        consulta = one(conn, service_sql, t=t, n="Consulta", d=30, b=0, a=15)
        banho = one(conn, service_sql, t=t, n="Banho", d=60, b=0, a=0)
        cirurgia = one(conn, service_sql, t=t, n="Cirurgia", d=60, b=30, a=0)
        for service in (consulta, banho):
            for provider in (ana, bruno):
                conn.execute(
                    sa.text("INSERT INTO service_providers VALUES (:s, :p, NULL)"),
                    {"s": service, "p": provider},
                )
        room = one(
            conn,
            "INSERT INTO resources (tenant_id, location_id, name, capacity) "
            "VALUES (:t, :l, 'Sala de banho', 1) RETURNING id",
            t=t,
            l=loc,
        )
        conn.execute(
            sa.text("INSERT INTO service_resources VALUES (:s, :r)"), {"s": banho, "r": room}
        )
        contact = one(
            conn,
            "INSERT INTO contacts (tenant_id, phone_e164, name) "
            "VALUES (:t, :p, 'Carla') RETURNING id",
            t=t,
            p=f"+5511{marker}",
        )
        conversation = one(
            conn,
            "INSERT INTO conversations (tenant_id, contact_id, channel, channel_thread) "
            "VALUES (:t, :c, 'whatsapp', :th) RETURNING id",
            t=t,
            c=contact,
            th=marker,
        )
        appt_sql = (
            "INSERT INTO appointments (tenant_id, contact_id, provider_id, resource_id, "
            "service_id, starts_at, ends_at, status) "
            "VALUES (:t, :c, :p, :r, :s, :a, :b, :st) RETURNING id"
        )
        # Ana, Consulta 09:00-09:30: com 15 de buffer, ocupa ate 09:45.
        one(conn, appt_sql, t=t, c=contact, p=ana, r=None, s=consulta, a=at(9), b=at(9, 30),
            st="confirmed")  # fmt: skip
        # Ana, cancelado 11:00-11:30: nao ocupa.
        one(conn, appt_sql, t=t, c=contact, p=ana, r=None, s=consulta, a=at(11), b=at(11, 30),
            st="cancelled")  # fmt: skip
        # Bruno, Cirurgia 11:00-12:00 com 30 de buffer antes: ocupa desde 10:30.
        one(conn, appt_sql, t=t, c=contact, p=bruno, r=None, s=cirurgia, a=at(11), b=at(12),
            st="confirmed")  # fmt: skip
        # Bruno, Banho 08:00-09:00 na sala: a sala fica ocupada para a Ana tambem.
        one(conn, appt_sql, t=t, c=contact, p=bruno, r=room, s=banho, a=at(8), b=at(9),
            st="confirmed")  # fmt: skip
        hold_sql = (
            "INSERT INTO holds (tenant_id, conversation_id, provider_id, service_id, "
            "starts_at, ends_at, expires_at) VALUES (:t, :cv, :p, :s, :a, :b, :x) RETURNING id"
        )
        # Ana, hold vigente 10:30-11:00 (+15 de buffer).
        one(conn, hold_sql, t=t, cv=conversation, p=ana, s=consulta, a=at(10, 30), b=at(11),
            x=NOW + timedelta(minutes=10))  # fmt: skip
        # Ana, hold vencido 11:30-12:00: nao ocupa, mesmo com status ainda `active`.
        one(conn, hold_sql, t=t, cv=conversation, p=ana, s=consulta, a=at(11, 30), b=at(12),
            x=NOW - timedelta(minutes=1))  # fmt: skip
    yield Seed(t, ana, bruno, room, consulta, banho, cirurgia)
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :t"), {"t": t})


async def _find(
    seed: Seed,
    config: TenantConfig,
    service: uuid.UUID,
    calendar: CalendarProvider | None,
    **kwargs: Any,
) -> AvailabilityResult:
    request = AvailabilityRequest(
        service_id=service, date_from=WED, date_to=WED, now=NOW, limit=100, **kwargs
    )

    def resolver(owner: Provider | Resource) -> CalendarProvider | None:
        return calendar

    async with tenant_session(seed.tenant) as session:
        return await find_availability(
            session, tenant_id=seed.tenant, config=config, request=request, calendars=resolver
        )


def _times(result: AvailabilityResult, provider: uuid.UUID | None = None) -> list[str]:
    return [
        slot.local_start.strftime("%H:%M")
        for slot in sorted(result.slots, key=lambda s: s.start)
        if provider is None or slot.provider_id == provider
    ]


async def test_ocupacao_do_banco_e_do_calendario_com_buffers(
    seed: Seed, config: TenantConfig
) -> None:
    calendar = FakeCalendar({"ana@cal": [Interval(at(8), at(8, 15))]})
    result = await _find(seed, config, seed.consulta, calendar, provider_id=seed.ana)  # type: ignore[arg-type]
    assert result.unavailable == {}
    # 08:00 ocupado no Google; 08:15 termina 08:45+15 = 09:00, encostando no agendamento.
    # 09:45 e o primeiro depois do agendamento estendido e encosta no hold (10:30).
    # O hold vigente, com buffer, vai ate 11:15. 11:15 serve porque o cancelado e o hold
    # vencido (11:30) nao ocupam: 11:15+30+15 = 12:00, fim da janela.
    assert _times(result) == ["08:15", "09:45", "11:15"]
    assert all(slot.end - slot.start == timedelta(minutes=30) for slot in result.slots)


async def test_buffer_do_servico_ja_marcado_protege_o_proximo(
    seed: Seed, config: TenantConfig
) -> None:
    result = await _find(seed, config, seed.consulta, None, provider_id=seed.bruno)
    # Cirurgia 11:00 com 30 min de preparo: Bruno esta ocupado desde 10:30.
    assert _times(result)[-1] == "09:45"  # 09:45-10:15 + 15 = 10:30


async def test_recurso_ocupado_por_um_profissional_bloqueia_o_outro(
    seed: Seed, config: TenantConfig
) -> None:
    calendar = FakeCalendar({})
    result = await _find(seed, config, seed.banho, calendar)  # type: ignore[arg-type]
    # Ana esta livre das 08:00 as 09:00, mas a unica sala esta com o Bruno. Depois disso
    # ela nao tem 60 min seguidos; o Bruno tem, a partir das 09:00, ate a cirurgia.
    assert _times(result) == ["09:00", "09:15", "09:30"]
    assert {slot.provider_id for slot in result.slots} == {seed.bruno}
    assert all(slot.resource_id == seed.room for slot in result.slots)


async def test_calendario_sem_resposta_tira_o_profissional_e_avisa(
    seed: Seed, config: TenantConfig
) -> None:
    calendar = FakeCalendar(CalendarSyncPending("google fora", attempts=5))
    result = await _find(seed, config, seed.consulta, calendar)  # type: ignore[arg-type]
    assert seed.ana in result.unavailable
    assert {slot.provider_id for slot in result.slots} == {seed.bruno}


async def test_uniao_dos_profissionais(seed: Seed, config: TenantConfig) -> None:
    result = await _find(seed, config, seed.consulta, FakeCalendar({}))  # type: ignore[arg-type]
    by_time = {s.local_start.strftime("%H:%M"): s.provider_id for s in result.slots}
    assert by_time["08:00"] == seed.ana  # Ana vem antes por nome
    assert by_time["09:00"] == seed.bruno  # Ana ocupada as 09:00
    assert len(by_time) == len(result.slots)


async def test_servico_desconhecido(seed: Seed, config: TenantConfig) -> None:
    with pytest.raises(UnknownService):
        await _find(seed, config, uuid.uuid4(), None)


async def test_servico_sem_profissional_apto_retorna_vazio(
    seed: Seed, config: TenantConfig
) -> None:
    result = await _find(seed, config, seed.cirurgia, None)
    assert result.slots == []
