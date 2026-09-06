"""Sessao de banco e contexto de tenant (RNF-03).

Como o isolamento funciona
--------------------------
Toda tabela com `tenant_id` tem RLS ligada e **forcada** (`FORCE ROW LEVEL SECURITY`).
Sem o FORCE, o dono das tabelas — que aqui e o mesmo usuario da aplicacao — passaria
por cima da politica e o isolamento seria decorativo.

A politica compara `tenant_id` com `current_setting('app.tenant_id', true)`. Com a
variavel nao definida, `current_setting` devolve NULL e a comparacao e falsa: o padrao
e **nao ver nada**. Esquecer de abrir o contexto nao vaza dado, apenas nao retorna linha.

`bypass_rls_session()` e a unica porta de saida, usada por migrations, onboarding e
jobs que atravessam tenants. Nao use em codigo de request.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        _engine = create_async_engine(
            str(settings.database_url),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            echo=settings.db_echo,
            pool_pre_ping=True,
        )
    return _engine


def get_sessionmaker(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(settings), expire_on_commit=False, autoflush=False
        )
    return _sessionmaker


async def dispose_engine() -> None:
    """Fecha o pool. Usado no shutdown e entre testes."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def _set_local(session: AsyncSession, key: str, value: str) -> None:
    """Define uma GUC no escopo da transacao.

    `SET LOCAL` nao aceita parametro vinculado, entao o valor precisa ser embutido.
    Por isso ele passa por `set_config`, que aceita bind e elimina o risco de injecao.
    """
    await session.execute(
        text("SELECT set_config(:key, :value, true)"), {"key": key, "value": value}
    )


@asynccontextmanager
async def tenant_session(
    tenant_id: uuid.UUID | str, settings: Settings | None = None
) -> AsyncIterator[AsyncSession]:
    """Sessao com RLS amarrada a um tenant.

    Abre transacao, fixa `app.tenant_id` e entrega a sessao. Commit no sucesso,
    rollback em excecao. A GUC e local a transacao: nao vaza para a proxima conexao
    tirada do pool.
    """
    factory = get_sessionmaker(settings)
    async with factory() as session, session.begin():
        await _set_local(session, "app.tenant_id", str(tenant_id))
        yield session


@asynccontextmanager
async def bypass_rls_session(
    settings: Settings | None = None,
) -> AsyncIterator[AsyncSession]:
    """Sessao que enxerga todos os tenants.

    Somente para migrations, onboarding de tenant, jobs de cron que varrem a base
    inteira e roteamento de webhook (que precisa descobrir o tenant antes de ter um).
    Nunca em caminho de request autenticado.
    """
    factory = get_sessionmaker(settings)
    async with factory() as session, session.begin():
        await _set_local(session, "app.bypass_rls", "on")
        yield session


async def current_tenant_id(session: AsyncSession) -> uuid.UUID | None:
    """Le o tenant ativo na sessao. Util em asserts e testes."""
    result = await session.execute(
        text("SELECT NULLIF(current_setting('app.tenant_id', true), '')")
    )
    value = result.scalar_one_or_none()
    return uuid.UUID(value) if value else None
