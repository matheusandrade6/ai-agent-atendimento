"""Ingestao e recuperacao da base de conhecimento contra Postgres real (secao 11.6).

Os tres pontos do aceite da sessao S07 so se provam com banco de verdade: dedup de
chunk na reingestao (existe linha de verdade para contar), isolamento entre tenants
(RLS + filtro explicito) e o corte de score vindo de uma similaridade calculada pelo
pgvector, nao simulada em memoria.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import sqlalchemy as sa

from app.core.config import Settings
from app.core.db import dispose_engine
from app.knowledge.embeddings import HashingEmbeddingProvider
from app.knowledge.ingest import ingest_document
from app.knowledge.retrieve import search_knowledge

pytestmark = pytest.mark.integration


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


def _chunk_count(conn: sa.engine.Connection, document_id: uuid.UUID) -> int:
    return conn.execute(
        sa.text("SELECT count(*) FROM knowledge_chunks WHERE document_id = :d"),
        {"d": document_id},
    ).scalar_one()


@pytest.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    """Evita `Event loop is closed`: ver `docs/DECISOES.md`, D-24."""
    await dispose_engine()
    yield
    await dispose_engine()


@pytest.fixture
def tenant_id(sync_engine: sa.Engine) -> Iterator[uuid.UUID]:
    marker = uuid.uuid4().hex[:8]
    with sync_engine.begin() as conn:
        _bypass(conn)
        tenant = _seed_tenant(conn, f"tenant-s07-{marker}")
    yield tenant
    with sync_engine.begin() as conn:
        _bypass(conn)
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :t"), {"t": tenant})


@pytest.fixture
def embeddings() -> HashingEmbeddingProvider:
    return HashingEmbeddingProvider()


CONTEUDO_LONGO = " ".join(f"palavra{i}" for i in range(600))


# ------------------------------- reingestao / dedup -------------------------------


async def test_reingestao_do_mesmo_documento_nao_duplica_chunks(
    sync_engine: sa.Engine,
    settings: Settings,
    tenant_id: uuid.UUID,
    embeddings: HashingEmbeddingProvider,
) -> None:
    with sync_engine.begin() as conn:
        _bypass(conn)
        document_id = _insert_document(conn, tenant_id, "FAQ", CONTEUDO_LONGO)

    primeira = await ingest_document(
        tenant_id, document_id, embeddings=embeddings, settings=settings
    )
    with sync_engine.connect() as conn:
        _bypass(conn)
        assert primeira == 2  # 600 palavras, chunk 500, overlap 80 -> 2 chunks
        assert _chunk_count(conn, document_id) == 2

    segunda = await ingest_document(
        tenant_id, document_id, embeddings=embeddings, settings=settings
    )
    with sync_engine.connect() as conn:
        _bypass(conn)
        assert segunda == 2
        assert _chunk_count(conn, document_id) == 2  # nao dobrou


async def test_reingestao_com_conteudo_editado_substitui_os_chunks_antigos(
    sync_engine: sa.Engine,
    settings: Settings,
    tenant_id: uuid.UUID,
    embeddings: HashingEmbeddingProvider,
) -> None:
    with sync_engine.begin() as conn:
        _bypass(conn)
        document_id = _insert_document(conn, tenant_id, "FAQ", CONTEUDO_LONGO)
    await ingest_document(tenant_id, document_id, embeddings=embeddings, settings=settings)

    with sync_engine.begin() as conn:
        _bypass(conn)
        conn.execute(
            sa.text("UPDATE knowledge_documents SET content = :c WHERE id = :d"),
            {"c": "conteudo bem mais curto depois da edicao", "d": document_id},
        )
    nova_contagem = await ingest_document(
        tenant_id, document_id, embeddings=embeddings, settings=settings
    )

    assert nova_contagem == 1
    with sync_engine.connect() as conn:
        _bypass(conn)
        assert _chunk_count(conn, document_id) == 1


# ------------------------------- isolamento de tenant -------------------------------


async def test_busca_com_tenant_a_nunca_retorna_chunk_de_b(
    sync_engine: sa.Engine, settings: Settings, embeddings: HashingEmbeddingProvider
) -> None:
    marker = uuid.uuid4().hex[:8]
    conteudo = "horario de funcionamento da clinica e das oito as dezoito horas"
    with sync_engine.begin() as conn:
        _bypass(conn)
        tenant_a = _seed_tenant(conn, f"tenant-a-s07-{marker}")
        tenant_b = _seed_tenant(conn, f"tenant-b-s07-{marker}")
        # Conteudo identico nos dois tenants: se o filtro de tenant falhar, a busca de A
        # devolveria o chunk de B tranquilamente, com o mesmo score.
        doc_a = _insert_document(conn, tenant_a, "FAQ A", conteudo)
        doc_b = _insert_document(conn, tenant_b, "FAQ B", conteudo)

    try:
        await ingest_document(tenant_a, doc_a, embeddings=embeddings, settings=settings)
        await ingest_document(tenant_b, doc_b, embeddings=embeddings, settings=settings)

        resultados_a = await search_knowledge(
            tenant_a,
            "qual o horario de funcionamento",
            embeddings=embeddings,
            min_score=0.0,
            settings=settings,
        )

        assert resultados_a
        assert all(r.document_id == doc_a for r in resultados_a)
    finally:
        with sync_engine.begin() as conn:
            _bypass(conn)
            conn.execute(
                sa.text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": [tenant_a, tenant_b]}
            )


# ------------------------------------ corte de score ------------------------------------


async def test_consulta_sem_match_retorna_vazio_em_vez_de_lixo(
    sync_engine: sa.Engine,
    settings: Settings,
    tenant_id: uuid.UUID,
    embeddings: HashingEmbeddingProvider,
) -> None:
    with sync_engine.begin() as conn:
        _bypass(conn)
        document_id = _insert_document(
            conn, tenant_id, "FAQ", "agendamos consultas veterinarias de segunda a sabado"
        )
    await ingest_document(tenant_id, document_id, embeddings=embeddings, settings=settings)

    relevante = await search_knowledge(
        tenant_id, "agendamos consultas veterinarias", embeddings=embeddings, settings=settings
    )
    assert relevante  # a mesma frase do documento precisa passar do corte

    irrelevante = await search_knowledge(
        tenant_id, "xyzabc quantum blockchain overclock", embeddings=embeddings, settings=settings
    )
    assert irrelevante == []
