"""Recuperacao da base de conhecimento — RAG (secao 11.6).

O corte de score vem depois do top-k, nao antes
-------------------------------------------------
`top_k` sempre traz os chunks mais proximos que existem para o tenant, por menor que
seja a semelhanca; so entao os que nao alcancam `min_score` sao descartados. Se nenhum
passar, a lista volta vazia — e `app.agent.prompt.block_knowledge` trata lista vazia
como "sem contexto" e instrui o modelo a declarar desconhecimento (RF-06) em vez de
responder com um trecho fracamente relacionado.

`tenant_id` e filtro explicito, nao so a RLS da sessao
---------------------------------------------------------
A mesma garantia dupla das outras tabelas com `tenant_id` (invariante 2): a RLS e a
rede de fundo, mas a query nunca depende so dela. E o que o aceite desta sessao cobre —
busca com tenant A nunca pode devolver chunk de B.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.core.config import Settings
from app.core.db import tenant_session
from app.domain.models import KnowledgeChunk, KnowledgeDocument
from app.knowledge.embeddings import EmbeddingProvider

__all__ = ["DEFAULT_MIN_SCORE", "TOP_K", "RetrievedChunk", "search_knowledge"]

TOP_K = 5
DEFAULT_MIN_SCORE = 0.5


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """Trecho recuperado, com score de similaridade por cosseno (1.0 = identico)."""

    document_id: uuid.UUID
    document_title: str
    content: str
    score: float


async def search_knowledge(
    tenant_id: uuid.UUID,
    query: str,
    *,
    embeddings: EmbeddingProvider,
    top_k: int = TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    settings: Settings | None = None,
) -> list[RetrievedChunk]:
    """Top-`top_k` por similaridade de cosseno, cortado em `min_score`.

    Consulta vazia nao gasta chamada de embedding: nao ha o que buscar.
    """
    if not query.strip():
        return []

    [query_vector] = await embeddings.embed([query])
    # `<=>` do pgvector e distancia de cosseno (0 = identico, 2 = oposto); a
    # similaridade que o corte de score compara e `1 - distancia`.
    distance = KnowledgeChunk.embedding.cosine_distance(list(query_vector))

    statement = (
        select(KnowledgeChunk, KnowledgeDocument.title, distance.label("distance"))
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(
            KnowledgeChunk.tenant_id == tenant_id,
            KnowledgeDocument.tenant_id == tenant_id,
        )
        .order_by(distance)
        .limit(top_k)
    )

    async with tenant_session(tenant_id, settings) as session:
        rows = (await session.execute(statement)).all()

    results: list[RetrievedChunk] = []
    for chunk, title, raw_distance in rows:
        score = 1.0 - float(raw_distance)
        if score < min_score:
            continue
        results.append(
            RetrievedChunk(
                document_id=chunk.document_id,
                document_title=title,
                content=chunk.content,
                score=score,
            )
        )
    return results
