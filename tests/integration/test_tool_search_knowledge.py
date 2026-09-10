"""Tool `search_knowledge` contra Postgres real (secao 11.4, RAG da 11.6).

O pipeline de ingestao/recuperacao ja tem cobertura propria em
`tests/integration/test_knowledge.py` (S07) — dedup, isolamento de tenant, corte de
score. Aqui o alvo e so a tool: schema, contexto e o formato que volta ao modelo.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from app.agent.tools.base import ToolContext
from app.agent.tools.search_knowledge import search_knowledge_tool
from app.core.config import Settings
from app.core.db import dispose_engine
from app.core.time import now_utc
from app.domain.tenant_config import TenantConfig
from app.knowledge.embeddings import HashingEmbeddingProvider
from app.knowledge.ingest import ingest_document

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


def _insert_document(
    conn: sa.engine.Connection, tenant_id: uuid.UUID, title: str, content: str
) -> uuid.UUID:
    return conn.execute(
        sa.text(
            "INSERT INTO knowledge_documents (tenant_id, title, content) "
            "VALUES (:t, :title, :content) RETURNING id"
        ),
        {"t": tenant_id, "title": title, "content": content},
    ).scalar_one()


@pytest.fixture
def tenant_id(sync_engine: sa.Engine) -> Iterator[uuid.UUID]:
    marker = uuid.uuid4().hex[:8]
    with sync_engine.begin() as conn:
        _bypass(conn)
        tenant = _seed_tenant(conn, f"tenant-s08-rag-{marker}")
    yield tenant
    with sync_engine.begin() as conn:
        _bypass(conn)
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :t"), {"t": tenant})


@pytest.fixture
def embeddings() -> HashingEmbeddingProvider:
    return HashingEmbeddingProvider()


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


async def test_tool_devolve_trecho_relevante_da_base(
    sync_engine: sa.Engine,
    settings: Settings,
    tenant_id: uuid.UUID,
    embeddings: HashingEmbeddingProvider,
    tenant_config: dict[str, Any],
) -> None:
    with sync_engine.begin() as conn:
        _bypass(conn)
        document_id = _insert_document(
            conn, tenant_id, "FAQ", "agendamos consultas veterinarias de segunda a sabado"
        )
    await ingest_document(tenant_id, document_id, embeddings=embeddings, settings=settings)

    tool = search_knowledge_tool(embeddings, settings=settings)
    result = await tool.handler(
        _context(tenant_id, _config(tenant_config)),
        {"query": "agendamos consultas veterinarias"},
    )

    results = result.content["results"]
    assert results
    assert results[0]["title"] == "FAQ"
    assert "consultas veterinarias" in results[0]["content"]


async def test_tool_sem_match_devolve_lista_vazia(
    sync_engine: sa.Engine,
    settings: Settings,
    tenant_id: uuid.UUID,
    embeddings: HashingEmbeddingProvider,
    tenant_config: dict[str, Any],
) -> None:
    with sync_engine.begin() as conn:
        _bypass(conn)
        document_id = _insert_document(
            conn, tenant_id, "FAQ", "horario de funcionamento e das oito as dezoito"
        )
    await ingest_document(tenant_id, document_id, embeddings=embeddings, settings=settings)

    tool = search_knowledge_tool(embeddings, settings=settings)
    result = await tool.handler(
        _context(tenant_id, _config(tenant_config)),
        {"query": "xyzabc quantum blockchain overclock"},
    )

    assert result.content["results"] == []
