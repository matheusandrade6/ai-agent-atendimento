"""`build_registry` liga tool a dependencia externa (D-27).

Sem `EmbeddingProvider`, `search_knowledge` fica de fora do catalogo em vez de rodar
sobre um provedor sem qualidade semantica (D-26) — o resto do conjunto continua
disponivel. As tools de agendamento (S15) entram sempre: sem calendario, so a agenda propria
aparece na oferta, o que e o lado seguro do erro.
"""

from __future__ import annotations

from app.agent.tools.registry import build_registry
from app.knowledge.embeddings import HashingEmbeddingProvider

SCHEDULING = {
    "check_availability",
    "hold_slot",
    "confirm_appointment",
    "find_appointments",
    "save_contact_info",
}


def test_sem_embeddings_search_knowledge_fica_de_fora() -> None:
    registry = build_registry(embeddings=None)

    assert "search_knowledge" not in registry.names()
    assert {"list_services", "escalate_to_human", *SCHEDULING} == set(registry.names())


def test_com_embeddings_search_knowledge_tambem_entra() -> None:
    registry = build_registry(embeddings=HashingEmbeddingProvider())

    assert {"list_services", "search_knowledge", "escalate_to_human", *SCHEDULING} == set(
        registry.names()
    )


def test_ordem_do_catalogo_e_estavel() -> None:
    """A lista de tools entra no prefixo cacheado do prompt; reordenar o invalida (D-21)."""
    first = build_registry(embeddings=HashingEmbeddingProvider()).names()
    second = build_registry(embeddings=HashingEmbeddingProvider()).names()

    assert first == second
    assert first[0] == "list_services"
    assert first[-1] == "search_knowledge"


def test_o_fluxo_de_reserva_tem_uma_tool_por_passo() -> None:
    registry = build_registry()

    for name in ("check_availability", "hold_slot", "confirm_appointment"):
        assert registry.get(name) is not None, name
