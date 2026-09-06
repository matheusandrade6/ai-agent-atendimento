from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import redis as redis_sync
import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import dispose_engine, get_sessionmaker
from app.domain.tenant_config import load_tenant_config

REPO_ROOT = Path(__file__).resolve().parent.parent
TENANTS_DIR = REPO_ROOT / "config" / "tenants"


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(environment="test", tenants_config_dir=str(TENANTS_DIR))


@pytest.fixture(scope="session")
def database_available(settings: Settings) -> bool:
    """Postgres acessivel? Sem ele, os testes de integracao sao pulados, nao falham."""
    if os.getenv("SKIP_DB_TESTS") == "1":
        return False
    engine = sa.create_engine(settings.sync_database_url, connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        engine.dispose()


@pytest.fixture
def require_db(database_available: bool) -> None:
    if not database_available:
        pytest.skip("Postgres indisponivel (docker compose up postgres)")


@pytest.fixture
def sync_engine(settings: Settings, require_db: None) -> Iterator[sa.Engine]:
    engine = sa.create_engine(settings.sync_database_url)
    yield engine
    engine.dispose()


@pytest.fixture
async def db_session(settings: Settings, require_db: None) -> AsyncIterator[AsyncSession]:
    """Sessao sem contexto de tenant. Serve para provar o comportamento fail-closed."""
    factory = get_sessionmaker(settings)
    async with factory() as session:
        yield session
    await dispose_engine()


@pytest.fixture(scope="session")
def redis_available(settings: Settings) -> bool:
    """Redis acessivel? Sem ele, os testes de fila sao pulados, nao falham."""
    if os.getenv("SKIP_REDIS_TESTS") == "1":
        return False

    client = redis_sync.Redis.from_url(str(settings.redis_url), socket_connect_timeout=3)
    try:
        client.ping()
        return True
    except Exception:
        return False
    finally:
        client.close()


@pytest.fixture
def require_redis(redis_available: bool) -> None:
    if not redis_available:
        pytest.skip("Redis indisponivel (docker compose up redis)")


@pytest.fixture
async def redis_client(settings: Settings, require_redis: None) -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(str(settings.redis_url))
    yield client
    await client.aclose()


@pytest.fixture(scope="session")
def tenant_config() -> dict[str, Any]:
    """Config do tenant de exemplo, ja validada, como dicionario cru.

    Serve de base para os tenants que os testes criam: em vez de inventar um YAML
    minimo que pode divergir do schema real, parte-se do fixture versionado.
    """
    return load_tenant_config(TENANTS_DIR / "clinica-exemplo.yaml").raw_dict()


@pytest.fixture
def tenant_whatsapp(
    sync_engine: sa.Engine, tenant_config: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    """Tenant ativo com WhatsApp habilitado, um contato e `debounce_seconds: 6`.

    Compartilhado pelos testes de fila: eles precisam de um tenant real no banco
    porque o worker resolve debounce e canal a partir da config persistida.
    """
    slug = f"s05-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"pnid-{uuid.uuid4().hex[:12]}"
    config = json.loads(json.dumps(tenant_config))
    config["channels"]["whatsapp"]["phone_number_id"] = phone_number_id

    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        tenant_id = conn.execute(
            sa.text(
                "INSERT INTO tenants (slug, name, vertical, config) "
                "VALUES (:slug, :slug, 'veterinaria', CAST(:config AS jsonb)) RETURNING id"
            ),
            {"slug": slug, "config": json.dumps(config)},
        ).scalar_one()
        contact_id = conn.execute(
            sa.text(
                "INSERT INTO contacts (tenant_id, phone_e164, name) "
                "VALUES (:t, :p, 'Carla') RETURNING id"
            ),
            {"t": tenant_id, "p": f"+5511{uuid.uuid4().int % 10**9:09d}"},
        ).scalar_one()

    yield {
        "tenant_id": tenant_id,
        "contact_id": contact_id,
        "phone_number_id": phone_number_id,
        "debounce_seconds": config["channels"]["whatsapp"]["debounce_seconds"],
    }

    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
