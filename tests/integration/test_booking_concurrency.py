"""Disputa pelo mesmo slot, com 20 pedidos de verdade em paralelo (13.4; aceite da S14).

Roda num Postgres descartavel do `testcontainers` (19.2), com as migrations aplicadas e o
papel sem privilegio da aplicacao (D-09) — RLS ligada, como em producao. Cada pedido tem
a sua conexao e a sua transacao; um `asyncio.Barrier` solta todos ao mesmo tempo.

Tres disputas, uma por camada:

1. 20 fluxos completos (hold e depois confirmacao) pelo mesmo horario: 1 agendamento.
2. 20 holds ja gravados para o mesmo horario — como se a camada 1 tivesse falhado —
   confirmados ao mesmo tempo: o advisory lock e a constraint deixam passar 1.
3. 20 confirmacoes do **mesmo** hold: 1 agendamento, e todas devolvem o mesmo id.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import date, time, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.time import combine_local
from app.scheduling.booking import ConfirmResult, confirm_appointment, hold_slot
from tests.conftest import REPO_ROOT
from tests.integration.booking_seed import BookingSeed, seed_booking

pytestmark = pytest.mark.integration

REQUESTS = 20
SP = "America/Sao_Paulo"
NOW = combine_local(date(2026, 11, 10), time(10, 0), SP)
SLOT = combine_local(date(2026, 11, 11), time(14, 0), SP)
TTL = timedelta(minutes=10)


@dataclass(frozen=True)
class Database:
    app_async_url: str
    app_sync_url: str


def _docker_running() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    """Postgres 16 + pgvector descartavel, com o papel da aplicacao e as migrations."""
    if not _docker_running():
        pytest.skip("Docker indisponivel: o teste de concorrencia sobe o proprio Postgres")
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # testcontainers < 4.10
        from testcontainers.postgres import PostgresContainer

    with PostgresContainer(
        "pgvector/pgvector:pg16",
        username="datamind",
        password="datamind",
        dbname="datamind",
        driver="psycopg2",
    ) as pg:
        host, port = pg.get_container_host_ip(), pg.get_exposed_port(5432)
        admin = sa.create_engine(pg.get_connection_url())
        init_sql = (REPO_ROOT / "docker" / "postgres" / "init" / "01-app-role.sql").read_text(
            encoding="utf-8"
        )
        with admin.begin() as conn:
            conn.exec_driver_sql(init_sql)
        admin.dispose()

        def url(user: str, driver: str) -> str:
            return f"postgresql+{driver}://{user}:{user}@{host}:{port}/datamind"

        with pytest.MonkeyPatch.context() as env:
            env.setenv("DATABASE_ADMIN_URL", url("datamind", "asyncpg"))
            env.setenv("DATABASE_URL", url("datamind_app", "asyncpg"))
            get_settings.cache_clear()
            config = Config()
            config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
            command.upgrade(config, "head")
        get_settings.cache_clear()

        yield Database(url("datamind_app", "asyncpg"), url("datamind_app", "psycopg2"))


@pytest.fixture
async def sessions(database: Database) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # Uma conexao por pedido: com pool menor, os pedidos fariam fila no pool e a disputa
    # deixaria de ser simultanea.
    engine = create_async_engine(database.app_async_url, pool_size=REQUESTS + 5, max_overflow=0)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def seed(database: Database) -> Iterator[BookingSeed]:
    engine = sa.create_engine(database.app_sync_url)
    yield seed_booking(engine, conversations=REQUESTS)
    engine.dispose()


def _count(database: Database, tenant: uuid.UUID, sql: str) -> int:
    engine = sa.create_engine(database.app_sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(tenant)}
            )
            return int(conn.execute(sa.text(sql)).scalar_one())
    finally:
        engine.dispose()


def _tenant_tx(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    @asynccontextmanager
    async def open_tx() -> AsyncIterator[AsyncSession]:
        async with factory() as session, session.begin():
            await session.execute(
                sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)}
            )
            yield session

    return open_tx


async def _all_at_once[T](work: Callable[[int], Awaitable[T]]) -> list[T]:
    barrier = asyncio.Barrier(REQUESTS)

    async def one(n: int) -> T:
        await barrier.wait()
        return await work(n)

    return await asyncio.wait_for(asyncio.gather(*(one(n) for n in range(REQUESTS))), 60)


async def test_20_fluxos_simultaneos_pelo_mesmo_slot_criam_um_agendamento(
    database: Database, sessions: async_sessionmaker[AsyncSession], seed: BookingSeed
) -> None:
    tx = _tenant_tx(sessions, seed.tenant)

    async def request(n: int) -> str:
        async with tx() as session:
            held = await hold_slot(
                session,
                tenant_id=seed.tenant,
                conversation_id=seed.conversations[n],
                provider_id=seed.ana,
                service_id=seed.consulta,
                starts_at=SLOT,
                now=NOW,
                ttl=TTL,
            )
        if held.hold_id is None:
            return held.status
        async with tx() as session:
            confirmed = await confirm_appointment(
                session, tenant_id=seed.tenant, hold_id=held.hold_id, now=NOW
            )
        return confirmed.status

    outcomes = await _all_at_once(request)

    assert sorted(outcomes) == ["confirmed"] + ["slot_taken"] * (REQUESTS - 1)
    assert _count(database, seed.tenant, "SELECT count(*) FROM appointments") == 1
    assert _count(database, seed.tenant, "SELECT count(*) FROM holds") == 1


async def test_20_holds_concorrentes_confirmados_juntos_deixam_passar_um(
    database: Database, sessions: async_sessionmaker[AsyncSession], seed: BookingSeed
) -> None:
    """Camada 1 furada de proposito: os holds entram sem checagem."""
    tx = _tenant_tx(sessions, seed.tenant)
    engine = sa.create_engine(database.app_sync_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(seed.tenant)}
        )
        hold_ids = [
            conn.execute(
                sa.text(
                    "INSERT INTO holds (tenant_id, conversation_id, provider_id, service_id, "
                    "starts_at, ends_at, expires_at) VALUES (:t, :c, :p, :s, :a, :b, :x) "
                    "RETURNING id"
                ),
                {
                    "t": seed.tenant,
                    "c": conversation,
                    "p": seed.ana,
                    "s": seed.consulta,
                    "a": SLOT + timedelta(minutes=n % 3 * 10),  # mesmos e quase mesmos
                    "b": SLOT + timedelta(minutes=n % 3 * 10 + 30),
                    "x": NOW + TTL,
                },
            ).scalar_one()
            for n, conversation in enumerate(seed.conversations)
        ]
    engine.dispose()

    async def request(n: int) -> str:
        async with tx() as session:
            result = await confirm_appointment(
                session, tenant_id=seed.tenant, hold_id=hold_ids[n], now=NOW
            )
        return result.status

    outcomes = await _all_at_once(request)

    assert sorted(outcomes) == ["confirmed"] + ["slot_taken"] * (REQUESTS - 1)
    assert _count(database, seed.tenant, "SELECT count(*) FROM appointments") == 1


async def test_20_confirmacoes_do_mesmo_hold_devolvem_o_mesmo_agendamento(
    database: Database, sessions: async_sessionmaker[AsyncSession], seed: BookingSeed
) -> None:
    tx = _tenant_tx(sessions, seed.tenant)
    async with tx() as session:
        held = await hold_slot(
            session,
            tenant_id=seed.tenant,
            conversation_id=seed.conversations[0],
            provider_id=seed.ana,
            service_id=seed.consulta,
            starts_at=SLOT,
            now=NOW,
            ttl=TTL,
        )
    assert held.hold_id is not None
    hold_id = held.hold_id

    async def request(n: int) -> ConfirmResult:
        async with tx() as session:
            return await confirm_appointment(
                session, tenant_id=seed.tenant, hold_id=hold_id, now=NOW
            )

    results = await _all_at_once(request)

    assert {r.status for r in results} == {"confirmed"}
    assert len({r.appointment_id for r in results}) == 1
    assert sum(not r.replayed for r in results) == 1
    assert _count(database, seed.tenant, "SELECT count(*) FROM appointments") == 1

    async with tx() as session:
        again = await confirm_appointment(session, tenant_id=seed.tenant, hold_id=hold_id, now=NOW)
    assert again.appointment_id == results[0].appointment_id
    assert _count(database, seed.tenant, "SELECT count(*) FROM appointments") == 1
