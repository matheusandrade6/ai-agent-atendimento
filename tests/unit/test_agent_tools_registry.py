"""`build_registry` liga tool a dependencia externa (D-27).

Sem `EmbeddingProvider`, `search_knowledge` fica de fora do catalogo em vez de rodar
sobre um provedor sem qualidade semantica (D-26) — o resto do conjunto continua
disponivel. As tools de agendamento (S15) e as de remarcar e cancelar (S16) entram sempre:
sem calendario, so a agenda propria aparece na oferta, o que e o lado seguro do erro.
"""

from __future__ import annotations

from app.agent.tools.registry import build_registry
from app.knowledge.embeddings import HashingEmbeddingProvider

SCHEDULING = {
    "check_availability",
    "hold_slot",
    "confirm_appointment",
    "find_appointments",
    "reschedule_appointment",
    "cancel_appointment",
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


def test_remarcar_e_cancelar_sao_escrita_e_nao_aceitam_tenant_nem_horario_livre() -> None:
    registry = build_registry()

    for name in ("reschedule_appointment", "cancel_appointment"):
        spec = registry.get(name)
        assert spec is not None, name
        assert spec.kind == "write", name
        assert "tenant_id" not in spec.input_schema["properties"], name
    reschedule = registry.get("reschedule_appointment")
    cancel = registry.get("cancel_appointment")
    assert reschedule is not None and cancel is not None
    # O horario novo so entra por `slot_token`: nao ha campo para o modelo escrever um horario.
    # O token e opcional de proposito (D-60): sem ele a tool so decide a politica.
    assert set(reschedule.input_schema["properties"]) == {"appointment_id", "slot_token"}
    assert reschedule.input_schema["required"] == ["appointment_id"]
    # Cancelar: a confirmacao e um token emitido pela propria tool, nao um booleano.
    assert set(cancel.input_schema["properties"]) == {
        "appointment_id",
        "confirmation_token",
        "reason",
    }
    assert cancel.input_schema["required"] == ["appointment_id"]


def test_find_appointments_vem_antes_de_remarcar_e_cancelar() -> None:
    """A ordem da 11.4: achar o agendamento, depois mexer nele. Tambem fixa o prefixo cacheado."""
    names = build_registry().names()

    assert names.index("find_appointments") < names.index("reschedule_appointment")
    assert names.index("reschedule_appointment") < names.index("cancel_appointment")
