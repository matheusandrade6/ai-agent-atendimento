from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import dispose_engine, get_sessionmaker

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
