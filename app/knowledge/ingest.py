"""Ingestao da base de conhecimento (secao 11.6).

Reingestao e substituicao, nao upsert
--------------------------------------
`ingest_document` apaga todos os chunks existentes do documento e insere os novos, na
mesma transacao. Nao ha upsert por `(document_id, ordinal)` porque o numero de chunks
muda sempre que o conteudo muda de tamanho — acompanhar esse diff custaria mais do que
vale. O efeito e o pedido do aceite desta sessao: reingerir um documento nunca duplica
chunk, seja o conteudo igual ou editado.

Chunking e aproximado, de proposito
------------------------------------
"~500 tokens" (11.6) e aproximado — o projeto nao tem tokenizador de LLM como
dependencia, e um chunk de RAG nao precisa da contagem exata que um limite de contexto
exigiria. `chunk_text` usa palavra (sequencia sem espaco) como unidade: simples,
determinístico, e suficiente para o overlap fazer o que precisa fazer, que e nao cortar
uma ideia no meio entre dois chunks vizinhos.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import delete

from app.core.config import Settings
from app.core.db import tenant_session
from app.core.telemetry import get_logger
from app.domain.models import KnowledgeChunk, KnowledgeDocument
from app.knowledge.embeddings import EmbeddingProvider

__all__ = [
    "CHUNK_OVERLAP_TOKENS",
    "CHUNK_TOKENS",
    "DocumentNotFound",
    "chunk_text",
    "ingest_document",
]

log = get_logger(__name__)

CHUNK_TOKENS = 500
CHUNK_OVERLAP_TOKENS = 80

_TOKEN_RE = re.compile(r"\S+")


class DocumentNotFound(Exception):
    def __init__(self, *, tenant_id: uuid.UUID, document_id: uuid.UUID) -> None:
        super().__init__(f"documento {document_id} nao encontrado para o tenant {tenant_id}")
        self.tenant_id = tenant_id
        self.document_id = document_id


def chunk_text(
    content: str, *, chunk_tokens: int = CHUNK_TOKENS, overlap_tokens: int = CHUNK_OVERLAP_TOKENS
) -> list[str]:
    """Fatia `content` em pedacos de ate `chunk_tokens` "tokens" (palavras), com
    `overlap_tokens` de sobreposicao entre pedacos vizinhos.

    Os limites de cada chunk caem em fronteira de palavra do texto original — nada de
    normalizar espacos ou juntar linhas — para que o conteudo recuperado depois seja
    legivel como veio do documento.
    """
    if chunk_tokens <= overlap_tokens:
        raise ValueError("chunk_tokens deve ser maior que overlap_tokens")

    words = list(_TOKEN_RE.finditer(content))
    if not words:
        return []

    step = chunk_tokens - overlap_tokens
    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + chunk_tokens, len(words))
        chunks.append(content[words[start].start() : words[end - 1].end()])
        if end == len(words):
            break
        start += step
    return chunks


async def ingest_document(
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    embeddings: EmbeddingProvider,
    settings: Settings | None = None,
) -> int:
    """(Re)calcula os chunks de um documento a partir do `content` ja persistido.

    Quem edita titulo/conteudo (painel, script de onboarding) grava o documento antes
    e so entao chama isto — `ingest_document` le o que esta no banco, nao recebe texto
    solto, para nunca divergir do que `knowledge_documents.content` realmente guarda.

    Devolve o numero de chunks gravados.
    """
    async with tenant_session(tenant_id, settings) as session:
        document = await session.get(KnowledgeDocument, document_id)
        if document is None or document.tenant_id != tenant_id:
            raise DocumentNotFound(tenant_id=tenant_id, document_id=document_id)

        chunks = chunk_text(document.content)

        # Tenant_id no filtro alem do `document_id`: nenhuma query sem tenant_id
        # (invariante 2), ainda que a RLS da sessao ja garanta o mesmo escopo.
        await session.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.tenant_id == tenant_id,
                KnowledgeChunk.document_id == document_id,
            )
        )
        if not chunks:
            log.info("documento_sem_conteudo_ingerivel", document_id=str(document_id))
            return 0

        vectors = await embeddings.embed(chunks)
        if len(vectors) != len(chunks):
            raise ValueError(
                "EmbeddingProvider devolveu um numero de vetores diferente do de chunks"
            )

        session.add_all(
            KnowledgeChunk(
                tenant_id=tenant_id,
                document_id=document_id,
                ordinal=ordinal,
                content=chunk_content,
                embedding=list(vector),
            )
            for ordinal, (chunk_content, vector) in enumerate(zip(chunks, vectors, strict=True))
        )

    log.info("documento_ingerido", document_id=str(document_id), chunks=len(chunks))
    return len(chunks)
