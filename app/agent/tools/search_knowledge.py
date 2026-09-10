"""Tool de leitura: busca na base de conhecimento do tenant (secao 11.4, RAG da 11.6).

Por que a tool nao chama `app.knowledge.retrieve` direto do motor
-------------------------------------------------------------------
O motor so conhece `ToolRegistry`/`ToolSpec`; quem sabe que a busca precisa de um
`EmbeddingProvider` (e, por tabela, de settings para abrir a sessao certa) e este
arquivo. `search_knowledge_tool` fecha essas dependencias e devolve um `ToolSpec`
pronto — o handler em si so ve `ToolContext` e os argumentos do modelo, como qualquer
outra tool.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from app.agent.tools.base import ToolContext, ToolResult, ToolSpec
from app.core.config import Settings
from app.knowledge.embeddings import EmbeddingProvider
from app.knowledge.retrieve import DEFAULT_MIN_SCORE, TOP_K, search_knowledge

__all__ = ["SearchKnowledgeArgs", "search_knowledge_tool"]

NAME = "search_knowledge"

_DESCRIPTION = (
    "Busca na base de conhecimento do tenant (FAQ, politicas, instrucoes de preparo, "
    "informacoes praticas). Use antes de responder qualquer duvida sobre o "
    "estabelecimento que voce nao tenha certeza. Se a busca nao trouxer nada relevante, "
    "diga que vai confirmar em vez de inventar."
)

_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "A duvida ou termo de busca, em linguagem natural.",
        }
    },
    "required": ["query"],
}


class SearchKnowledgeArgs(BaseModel):
    query: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class _Handler:
    embeddings: EmbeddingProvider
    settings: Settings | None
    top_k: int
    min_score: float

    async def __call__(self, ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        chunks = await search_knowledge(
            ctx.tenant_id,
            str(arguments["query"]),
            embeddings=self.embeddings,
            top_k=self.top_k,
            min_score=self.min_score,
            settings=self.settings,
        )
        return ToolResult(
            content={
                "results": [
                    {
                        "title": chunk.document_title,
                        "content": chunk.content,
                        "score": round(chunk.score, 3),
                    }
                    for chunk in chunks
                ]
            }
        )


def search_knowledge_tool(
    embeddings: EmbeddingProvider, *, settings: Settings | None = None
) -> ToolSpec:
    top_k = settings.knowledge_top_k if settings is not None else TOP_K
    min_score = settings.knowledge_min_score if settings is not None else DEFAULT_MIN_SCORE
    handler = _Handler(embeddings=embeddings, settings=settings, top_k=top_k, min_score=min_score)
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema=_INPUT_SCHEMA,
        handler=handler,
        kind="read",
        args_model=SearchKnowledgeArgs,
    )
