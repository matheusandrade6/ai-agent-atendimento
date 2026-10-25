"""Hold, confirmacao e expiracao contra Postgres real (13.4; Sessao S14).

Comportamento um pedido por vez: o que vira `held`, `slot_taken`, `invalid`,
`hold_expired`; a confirmacao idempotente; e a camada 3 (`appt_no_overlap`) traduzida
para `slot_taken` quando uma escrita que nao passou pelo lock chega primeiro. A disputa de
20 pedidos simultaneos esta em `test_booking_concurrency.py`, com testcontainers.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime, time, timedelta

import pytest
import sqlalchemy as sa

from app.core.db import bypass_rls_session, dispose_engine, get_sessionmaker, tenant_session
from app.core.time import combine_local
from app.scheduling.booking import (
    ConfirmResult,
    HoldResult,
    confirm_appointment,
    expire_holds,
    hold_slot,
    idempotency_key_for,
)
from tests.integration.booking_seed import BookingSeed, drop_tenant, seed_booking

pytestmark = pytest.mark.integration

SP = "America/Sao_Paulo"
DAY = date(2026, 11, 11)
NOW = combine_local(date(2026, 11, 10), time(10, 0), SP)
TTL = timedelta(minutes=10)


def at(hour: int, minute: int = 0) -> datetime:
    return combine_local(DAY, time(hour, minute), SP)


@pytest.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    await dispose_engine()
    yield
    await dispose_engine()


@pytest.fixture
def seed(sync_engine: sa.Engine) -> Iterator[BookingSeed]:
    data = seed_booking(sync_engine, conversations=3)
    yield data
    drop_tenant(sync_engine, data.tenant)


async def hold(
    seed: BookingSeed,
    start: datetime,
    *,
    conversation: int = 0,
    provider: uuid.UUID | None = None,
    service: uuid.UUID | None = None,
    resource: uuid.UUID | None = None,
    now: datetime = NOW,
) -> HoldResult:
    async with tenant_session(seed.tenant) as session:
        return await hold_slot(
            session,
            tenant_id=seed.tenant,
            conversation_id=seed.conversations[conversation],
            provider_id=provider or seed.ana,
            service_id=service or seed.consulta,
            starts_at=start,
            now=now,
            ttl=TTL,
            resource_id=resource,
        )


async def confirm(seed: BookingSeed, hold_id: uuid.UUID, now: datetime = NOW) -> ConfirmResult:
    async with tenant_session(seed.tenant) as session:
        return await confirm_appointment(
            session, tenant_id=seed.tenant, hold_id=hold_id, now=now, intake={"pet": "Rex"}
        )


def _scalar(engine: sa.Engine, tenant: uuid.UUID, sql: str, **params: object) -> object:
    with engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(tenant)})
        return conn.execute(sa.text(sql), params).scalar_one()


def appointments(engine: sa.Engine, tenant: uuid.UUID) -> int:
    return int(_scalar(engine, tenant, "SELECT count(*) FROM appointments"))  # type: ignore[call-overload]


def hold_status(engine: sa.Engine, tenant: uuid.UUID, hold_id: uuid.UUID) -> object:
    return _scalar(engine, tenant, "SELECT status FROM holds WHERE id = :h", h=hold_id)


# ------------------------------------ hold ------------------------------------


async def test_hold_e_confirmacao(seed: BookingSeed, sync_engine: sa.Engine) -> None:
    held = await hold(seed, at(14))
    assert held.status == "held"
    assert held.ends_at == at(14, 30)  # duracao vem do banco, nao de quem chama
    assert held.expires_at == NOW + TTL
    assert held.hold_id is not None

    result = await confirm(seed, held.hold_id)
    assert result.status == "confirmed"
    assert not result.replayed
    assert result.as_dict() == {"status": "confirmed", "appointment_id": str(result.appointment_id)}

    with sync_engine.begin() as conn:
        conn.execute(
            sa.text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(seed.tenant)}
        )
        row = conn.execute(
            sa.text(
                "SELECT starts_at, ends_at, status, sync_status, idempotency_key, contact_id, "
                "location_id, conversation_id, source, intake FROM appointments WHERE id = :a"
            ),
            {"a": result.appointment_id},
        ).one()
    assert (row.starts_at, row.ends_at) == (at(14), at(14, 30))
    assert (row.status, row.source) == ("confirmed", "agent")
    assert row.sync_status == "pending_sync"  # o push ao calendario e depois do commit
    assert row.idempotency_key == idempotency_key_for(held.hold_id)
    assert (row.contact_id, row.location_id) == (seed.contact, seed.location)
    assert row.conversation_id == seed.conversations[0]
    assert row.intake == {"pet": "Rex"}
    assert hold_status(sync_engine, seed.tenant, held.hold_id) == "consumed"


async def test_confirmar_de_novo_devolve_o_mesmo_agendamento(
    seed: BookingSeed, sync_engine: sa.Engine
) -> None:
    held = await hold(seed, at(14))
    assert held.hold_id is not None
    first = await confirm(seed, held.hold_id)
    again = await confirm(seed, held.hold_id)
    later = await confirm(seed, held.hold_id, now=NOW + timedelta(hours=2))  # hold ja vencido
    assert again.appointment_id == first.appointment_id == later.appointment_id
    assert again.replayed and later.replayed
    assert appointments(sync_engine, seed.tenant) == 1


async def test_hold_vigente_bloqueia_o_mesmo_profissional_e_nao_o_outro(seed: BookingSeed) -> None:
    assert (await hold(seed, at(14))).status == "held"
    assert (await hold(seed, at(14, 15), conversation=1)).status == "slot_taken"
    assert (await hold(seed, at(14), conversation=1, provider=seed.bruno)).status == "held"


async def test_buffer_conta_no_hold(seed: BookingSeed) -> None:
    """Consulta 14:00-14:30 + 10 de buffer: 14:30 bate, 14:40 encosta."""
    assert (await hold(seed, at(14))).status == "held"
    assert (await hold(seed, at(14, 30), conversation=1)).status == "slot_taken"
    assert (await hold(seed, at(14, 40), conversation=1)).status == "held"


async def test_hold_vencido_nao_bloqueia(seed: BookingSeed) -> None:
    assert (await hold(seed, at(14))).status == "held"
    later = NOW + TTL + timedelta(minutes=1)
    assert (await hold(seed, at(14), conversation=1, now=later)).status == "held"


async def test_confirmar_hold_vencido(seed: BookingSeed, sync_engine: sa.Engine) -> None:
    held = await hold(seed, at(14))
    assert held.hold_id is not None
    result = await confirm(seed, held.hold_id, now=NOW + TTL)
    assert result.status == "hold_expired"
    assert hold_status(sync_engine, seed.tenant, held.hold_id) == "expired"
    assert appointments(sync_engine, seed.tenant) == 0


async def test_confirmar_hold_inexistente(seed: BookingSeed) -> None:
    assert (await confirm(seed, uuid.uuid4())).status == "hold_not_found"


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("passado", "horario no passado"),
        ("profissional", "profissional nao atende o servico"),
        ("sem_recurso", "servico exige um recurso apto"),
        ("recurso_sobrando", "servico nao usa recurso"),
        ("servico", "servico inexistente ou inativo"),
    ],
)
async def test_hold_invalido(seed: BookingSeed, case: str, reason: str) -> None:
    kwargs: dict[str, object] = {}
    start = at(14)
    if case == "passado":
        start = NOW - timedelta(minutes=1)
    elif case == "profissional":
        kwargs["provider"] = uuid.uuid4()
    elif case == "sem_recurso":
        kwargs["service"] = seed.banho
    elif case == "recurso_sobrando":
        kwargs["resource"] = seed.room
    elif case == "servico":
        kwargs["service"] = uuid.uuid4()
    result = await hold(seed, start, **kwargs)  # type: ignore[arg-type]
    assert (result.status, result.reason) == ("invalid", reason)


async def test_recurso_disputado_entre_profissionais(seed: BookingSeed) -> None:
    first = await hold(seed, at(9), service=seed.banho, resource=seed.room)
    assert first.status == "held"
    taken = await hold(
        seed, at(9, 30), conversation=1, provider=seed.bruno, service=seed.banho, resource=seed.room
    )
    assert taken.status == "slot_taken"
    free = await hold(
        seed, at(10), conversation=1, provider=seed.bruno, service=seed.banho, resource=seed.room
    )
    assert free.status == "held"


# ---------------------------------- confirmacao ----------------------------------


def _insert_human_appointment(
    conn: sa.Connection, seed: BookingSeed, start: datetime, end: datetime
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO appointments (tenant_id, contact_id, provider_id, service_id, "
            "starts_at, ends_at, source) VALUES (:t, :c, :p, :s, :a, :b, 'human')"
        ),
        {
            "t": seed.tenant,
            "c": seed.contact,
            "p": seed.ana,
            "s": seed.consulta,
            "a": start,
            "b": end,
        },
    )


async def test_confirmacao_confere_de_novo_e_recusa_o_que_entrou_depois_do_hold(
    seed: BookingSeed, sync_engine: sa.Engine
) -> None:
    """Agendamento feito pelo painel em cima do hold: so o buffer se sobrepoe."""
    held = await hold(seed, at(14))
    assert held.hold_id is not None
    with sync_engine.begin() as conn:
        conn.execute(
            sa.text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(seed.tenant)}
        )
        _insert_human_appointment(conn, seed, at(14, 35), at(15))
    result = await confirm(seed, held.hold_id)
    assert result.status == "slot_taken"
    assert hold_status(sync_engine, seed.tenant, held.hold_id) == "expired"
    assert appointments(sync_engine, seed.tenant) == 1  # so o do painel


async def test_constraint_de_exclusao_vira_slot_taken(
    seed: BookingSeed, sync_engine: sa.Engine
) -> None:
    """Camada 3: uma escrita que nao pegou o advisory lock chega primeiro.

    A transacao X insere o agendamento conflitante e **nao** faz commit. A confirmacao
    passa pela checagem (nao enxerga linha sem commit) e para no `INSERT`, esperando a
    `appt_no_overlap` decidir. X faz commit; o banco recusa a confirmacao, e a recusa
    precisa sair como `slot_taken` — nao como excecao.
    """
    held = await hold(seed, at(15))
    assert held.hold_id is not None

    factory = get_sessionmaker()
    async with factory() as other, factory() as observer:
        await other.begin()
        await other.execute(
            sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(seed.tenant)}
        )
        await other.execute(
            sa.text(
                "INSERT INTO appointments (tenant_id, contact_id, provider_id, service_id, "
                "starts_at, ends_at, source) VALUES (:t, :c, :p, :s, :a, :b, 'human')"
            ),
            {
                "t": seed.tenant,
                "c": seed.contact,
                "p": seed.ana,
                "s": seed.consulta,
                "a": at(15, 15),
                "b": at(15, 45),
            },
        )
        task = asyncio.create_task(confirm(seed, held.hold_id))
        for _ in range(200):
            waiting = await observer.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                    "AND query ILIKE 'INSERT INTO appointments%'"
                )
            )
            await observer.rollback()
            if waiting:
                break
            await asyncio.sleep(0.025)
        else:
            pytest.fail("a confirmacao nunca chegou a esperar pela constraint")
        await other.commit()
        result = await asyncio.wait_for(task, timeout=10)

    assert result.status == "slot_taken"
    assert hold_status(sync_engine, seed.tenant, held.hold_id) == "expired"
    assert appointments(sync_engine, seed.tenant) == 1


# ----------------------------------- expiracao -----------------------------------


async def test_job_de_expiracao_marca_so_os_vencidos(
    seed: BookingSeed, sync_engine: sa.Engine
) -> None:
    old = await hold(seed, at(9))
    fresh = await hold(seed, at(16), conversation=1, now=NOW + timedelta(minutes=8))
    assert old.hold_id is not None and fresh.hold_id is not None
    async with bypass_rls_session() as session:
        expired = await expire_holds(session, NOW + TTL)
    assert expired >= 1  # o banco e compartilhado com outros testes
    assert hold_status(sync_engine, seed.tenant, old.hold_id) == "expired"
    assert hold_status(sync_engine, seed.tenant, fresh.hold_id) == "active"
