"""`build_registry` liga tool a dependencia externa (D-27).

Sem `EmbeddingProvider`, `search_knowledge` fica de fora do catalogo em vez de rodar
sobre um provedor sem qualidade semantica (D-26) — o resto do conjunto continua
disponivel.
"""

from __future__ import annotations

from app.agent.tools.registry import build_registry
from app.knowledge.embeddings import HashingEmbeddingProvider


def test_sem_embeddings_so_list_services_entra() -> None:
    registry = build_registry(embeddings=None)
    assert registry.names() == ("list_services",)


def test_com_embeddings_search_knowledge_tambem_entra() -> None:
    registry = build_registry(embeddings=HashingEmbeddingProvider())
    assert set(registry.names()) == {"list_services", "search_knowledge"}
