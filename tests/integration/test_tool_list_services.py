"""Tool `list_services` contra Postgres real (secao 11.4, aceite da S08).

O ponto central e o casamento por `services.aliases`: o cliente pede pelo apelido do
servico, nao pelo nome cadastrado.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from app.agent.tools.base import ToolContext
from app.agent.tools.list_services import list_services_tool
from app.core.config import Settings
from app.core.db import dispose_engine
from app.core.time import now_utc
from app.domain.tenant_config import TenantConfig

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    """Evita `Event loop is closed`: ver `docs/DECISOES.md`, D-24."""
    await dispose_engine()
    yield
    await dispose_engine()


def _bypass(conn: sa.engine.Connection) -> None:
    conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))


def _seed_tenant(conn: sa.engine.Connection, slug: str) -> uuid.UUID:
    return conn.execute(
        sa.text(
            "INSERT INTO tenants (slug, name, vertical, config) "
            "VALUES (:slug, :slug, 'veterinaria', '{}'::jsonb) RETURNING id"
        ),
        {"slug": slug},
    ).scalar_one()


def _seed_service(
    conn: sa.engine.Connection,
    tenant_id: uuid.UUID,
    *,
    name: str,
    aliases: list[str],
    duration_minutes: int = 30,
    price_cents: int | None = None,
    price_note: str | None = None,
    active: bool = True,
) -> uuid.UUID:
    return conn.execute(
        sa.text(
            "INSERT INTO services (tenant_id, name, aliases, duration_minutes,"
            " price_cents, price_note, active)"
            " VALUES (:t, :name, :aliases, :duration, :price_cents, :price_note, :active)"
            " RETURNING id"
        ),
        {
            "t": tenant_id,
            "name": name,
            "aliases": aliases,
            "duration": duration_minutes,
            "price_cents": price_cents,
            "price_note": price_note,
            "active": active,
        },
    ).scalar_one()


@pytest.fixture
def tenant_id(sync_engine: sa.Engine) -> Iterator[uuid.UUID]:
    marker = uuid.uuid4().hex[:8]
    with sync_engine.begin() as conn:
        _bypass(conn)
        tenant = _seed_tenant(conn, f"tenant-s08-{marker}")
    yield tenant
    with sync_engine.begin() as conn:
        _bypass(conn)
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :t"), {"t": tenant})


def _context(tenant_id: uuid.UUID, config: TenantConfig) -> ToolContext:
    return ToolContext(
        tenant_id=tenant_id,
        conversation_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        channel="whatsapp",
        config=config,
        now=now_utc(),
    )


def _config(tenant_config: dict[str, Any]) -> TenantConfig:
    return TenantConfig.model_validate(tenant_config).resolved()


# ------------------------------- catalogo e filtro -------------------------------


async def test_lista_o_catalogo_inteiro_sem_filtro(
    sync_engine: sa.Engine,
    settings: Settings,
    tenant_id: uuid.UUID,
    tenant_config: dict[str, Any],
) -> None:
    with sync_engine.begin() as conn:
        _bypass(conn)
        _seed_service(conn, tenant_id, name="Consulta", aliases=[], price_cents=15000)
        _seed_service(
            conn, tenant_id, name="Vacina V10", aliases=["vacina"], price_note="a combinar"
        )

    tool = list_services_tool(settings=settings)
    result = await tool.handler(_context(tenant_id, _config(tenant_config)), {})

    names = {s["name"] for s in result.content["services"]}
    assert names == {"Consulta", "Vacina V10"}


async def test_filtro_casa_por_sinonimo_configurado(
    sync_engine: sa.Engine,
    settings: Settings,
    tenant_id: uuid.UUID,
    tenant_config: dict[str, Any],
) -> None:
    with sync_engine.begin() as conn:
        _bypass(conn)
        _seed_service(conn, tenant_id, name="Banho e tosa", aliases=["tosa", "banho"])
        _seed_service(conn, tenant_id, name="Consulta de rotina", aliases=[])

    tool = list_services_tool(settings=settings)
    result = await tool.handler(_context(tenant_id, _config(tenant_config)), {"query": "tosa"})

    assert [s["name"] for s in result.content["services"]] == ["Banho e tosa"]


async def test_filtro_casa_frase_inteira_contendo_o_sinonimo(
    sync_engine: sa.Engine,
    settings: Settings,
    tenant_id: uuid.UUID,
    tenant_config: dict[str, Any],
) -> None:
    """O modelo tanto manda um termo curto quanto repete um trecho da fala da pessoa."""
    with sync_engine.begin() as conn:
        _bypass(conn)
        _seed_service(conn, tenant_id, name="Banho e tosa", aliases=["tosa"])

    tool = list_services_tool(settings=settings)
    result = await tool.handler(
        _context(tenant_id, _config(tenant_config)), {"query": "quero marcar uma tosa"}
    )

    assert [s["name"] for s in result.content["services"]] == ["Banho e tosa"]


async def test_filtro_e_insensivel_a_acento_e_caixa(
    sync_engine: sa.Engine,
    settings: Settings,
    tenant_id: uuid.UUID,
    tenant_config: dict[str, Any],
) -> None:
    with sync_engine.begin() as conn:
        _bypass(conn)
        _seed_service(conn, tenant_id, name="Castração", aliases=["castracao"])

    tool = list_services_tool(settings=settings)
    result = await tool.handler(_context(tenant_id, _config(tenant_config)), {"query": "CASTRACAO"})

    assert [s["name"] for s in result.content["services"]] == ["Castração"]


async def test_filtro_sem_correspondencia_devolve_vazio(
    sync_engine: sa.Engine,
    settings: Settings,
    tenant_id: uuid.UUID,
    tenant_config: dict[str, Any],
) -> None:
    with sync_engine.begin() as conn:
        _bypass(conn)
        _seed_service(conn, tenant_id, name="Consulta", aliases=[])

    tool = list_services_tool(settings=settings)
    result = await tool.handler(
        _context(tenant_id, _config(tenant_config)), {"query": "cirurgia cardiaca"}
    )

    assert result.content["services"] == []


async def test_servico_inativo_nao_aparece(
    sync_engine: sa.Engine,
    settings: Settings,
    tenant_id: uuid.UUID,
    tenant_config: dict[str, Any],
) -> None:
    with sync_engine.begin() as conn:
        _bypass(conn)
        _seed_service(conn, tenant_id, name="Descontinuado", aliases=[], active=False)

    tool = list_services_tool(settings=settings)
    result = await tool.handler(_context(tenant_id, _config(tenant_config)), {})

    assert result.content["services"] == []


async def test_preco_formatado_em_reais_ou_nota(
    sync_engine: sa.Engine,
    settings: Settings,
    tenant_id: uuid.UUID,
    tenant_config: dict[str, Any],
) -> None:
    with sync_engine.begin() as conn:
        _bypass(conn)
        _seed_service(conn, tenant_id, name="Consulta", aliases=[], price_cents=15050)
        _seed_service(conn, tenant_id, name="Cirurgia", aliases=[], price_note="sob avaliacao")

    tool = list_services_tool(settings=settings)
    result = await tool.handler(_context(tenant_id, _config(tenant_config)), {})

    by_name = {s["name"]: s["price_line"] for s in result.content["services"]}
    assert by_name["Consulta"] == "R$ 150,50"
    assert by_name["Cirurgia"] == "sob avaliacao"


# ------------------------------- isolamento de tenant -------------------------------


async def test_tenant_a_nao_ve_servico_de_b(
    sync_engine: sa.Engine, settings: Settings, tenant_config: dict[str, Any]
) -> None:
    marker = uuid.uuid4().hex[:8]
    with sync_engine.begin() as conn:
        _bypass(conn)
        tenant_a = _seed_tenant(conn, f"tenant-a-s08-{marker}")
        tenant_b = _seed_tenant(conn, f"tenant-b-s08-{marker}")
        _seed_service(conn, tenant_b, name="Servico de B", aliases=[])

    try:
        tool = list_services_tool(settings=settings)
        result = await tool.handler(_context(tenant_a, _config(tenant_config)), {})
        assert result.content["services"] == []
    finally:
        with sync_engine.begin() as conn:
            _bypass(conn)
            conn.execute(
                sa.text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": [tenant_a, tenant_b]}
            )
