"""Monta o `ToolRegistry` disponivel para o motor (secao 11.4).

Cada tool mora no seu proprio arquivo, com schema, handler e teste juntos — a regra de
ouro de `CLAUDE.md`. Este modulo so faz a ligacao: quem depende de infraestrutura
externa (embeddings, banco) recebe essa dependencia aqui, nunca dentro do motor.

`search_knowledge` e opcional de proposito: sem um `EmbeddingProvider` (por exemplo,
fora de dev/teste, onde `HashingEmbeddingProvider` nao pode ser usado — D-26), o motor
continua rodando com o resto do catalogo de tools, so sem RAG.

Tools de escrita (`hold_slot`, `confirm_appointment`, ...) entram nas sessoes seguintes
(S15) e se registram aqui do mesmo jeito.
"""

from __future__ import annotations

from app.agent.tools.base import ToolRegistry
from app.agent.tools.list_services import list_services_tool
from app.agent.tools.search_knowledge import search_knowledge_tool
from app.core.config import Settings
from app.knowledge.embeddings import EmbeddingProvider

__all__ = ["build_registry"]


def build_registry(
    *, embeddings: EmbeddingProvider | None = None, settings: Settings | None = None
) -> ToolRegistry:
    """Tools de leitura disponiveis hoje. Chamado uma vez por processo, no startup."""
    specs = [list_services_tool(settings=settings)]
    if embeddings is not None:
        specs.append(search_knowledge_tool(embeddings, settings=settings))
    return ToolRegistry(tuple(specs))
