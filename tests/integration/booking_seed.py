"""Massa de dados da S14, compartilhada pelos testes de booking (compose e testcontainers)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa


@dataclass
class BookingSeed:
    tenant: uuid.UUID
    location: uuid.UUID
    ana: uuid.UUID
    bruno: uuid.UUID
    contact: uuid.UUID
    #: Consulta: 30 min + 10 de buffer depois. Ana e Bruno atendem.
    consulta: uuid.UUID
    #: Banho: 60 min, exige a sala (capacidade 1). Ana e Bruno atendem.
    banho: uuid.UUID
    room: uuid.UUID
    conversations: list[uuid.UUID] = field(default_factory=list)


def _one(conn: sa.Connection, sql: str, **params: Any) -> uuid.UUID:
    value: uuid.UUID = conn.execute(sa.text(sql), params).scalar_one()
    return value


def seed_booking(engine: sa.Engine, *, conversations: int = 1) -> BookingSeed:
    """Cria um tenant isolado. `engine` precisa poder ligar `app.bypass_rls`."""
    marker = uuid.uuid4().hex[:8]
    with engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        t = _one(
            conn,
            "INSERT INTO tenants (slug, name, vertical, config) "
            "VALUES (:s, :s, 'veterinaria', '{}'::jsonb) RETURNING id",
            s=f"s14-{marker}",
        )
        loc = _one(
            conn,
            "INSERT INTO locations (tenant_id, name, timezone, business_hours) "
            "VALUES (:t, 'Unidade', 'America/Sao_Paulo', '{}'::jsonb) RETURNING id",
            t=t,
        )
        provider_sql = (
            "INSERT INTO providers (tenant_id, location_id, name, working_hours) "
            "VALUES (:t, :l, :n, '{}'::jsonb) RETURNING id"
        )
        ana = _one(conn, provider_sql, t=t, l=loc, n="Ana")
        bruno = _one(conn, provider_sql, t=t, l=loc, n="Bruno")
        service_sql = (
            "INSERT INTO services (tenant_id, name, duration_minutes, buffer_after_min) "
            "VALUES (:t, :n, :d, :a) RETURNING id"
        )
        consulta = _one(conn, service_sql, t=t, n="Consulta", d=30, a=10)
        banho = _one(conn, service_sql, t=t, n="Banho", d=60, a=0)
        for service in (consulta, banho):
            for provider in (ana, bruno):
                conn.execute(
                    sa.text("INSERT INTO service_providers VALUES (:s, :p, NULL)"),
                    {"s": service, "p": provider},
                )
        room = _one(
            conn,
            "INSERT INTO resources (tenant_id, location_id, name, capacity) "
            "VALUES (:t, :l, 'Sala', 1) RETURNING id",
            t=t,
            l=loc,
        )
        conn.execute(
            sa.text("INSERT INTO service_resources VALUES (:s, :r)"), {"s": banho, "r": room}
        )
        contact = _one(
            conn,
            "INSERT INTO contacts (tenant_id, phone_e164, name) "
            "VALUES (:t, :p, 'Carla') RETURNING id",
            t=t,
            p=f"+5511{marker}",
        )
        threads = [
            _one(
                conn,
                "INSERT INTO conversations (tenant_id, contact_id, channel, channel_thread) "
                "VALUES (:t, :c, 'whatsapp', :th) RETURNING id",
                t=t,
                c=contact,
                th=f"{marker}-{n}",
            )
            for n in range(conversations)
        ]
    return BookingSeed(t, loc, ana, bruno, contact, consulta, banho, room, threads)


def drop_tenant(engine: sa.Engine, tenant: uuid.UUID) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        for table in ("appointments", "holds", "conversations", "contacts"):
            conn.execute(sa.text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(
            sa.text(
                "DELETE FROM service_resources WHERE service_id IN "
                "(SELECT id FROM services WHERE tenant_id = :t)"
            ),
            {"t": tenant},
        )
        conn.execute(
            sa.text(
                "DELETE FROM service_providers WHERE service_id IN "
                "(SELECT id FROM services WHERE tenant_id = :t)"
            ),
            {"t": tenant},
        )
        for table in ("resources", "services", "providers", "locations"):
            conn.execute(sa.text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :t"), {"t": tenant})
