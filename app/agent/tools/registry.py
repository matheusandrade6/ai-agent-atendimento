"""Monta o `ToolRegistry` disponivel para o motor (secao 11.4).

Cada tool mora no seu proprio arquivo, com schema, handler e teste juntos — a regra de
ouro de `CLAUDE.md`. Este modulo so faz a ligacao: quem depende de infraestrutura
externa (embeddings, calendario, banco) recebe essa dependencia aqui, nunca dentro do motor.

`search_knowledge` e opcional de proposito: sem um `EmbeddingProvider` (por exemplo,
fora de dev/teste, onde `HashingEmbeddingProvider` nao pode ser usado — D-26), o motor
continua rodando com o resto do catalogo de tools, so sem RAG.

`escalate_to_human` (S10) nao depende de nada externo — entra sempre, mesmo sem
embeddings, porque e a unica via do modelo para pedir handoff (11.5, RF-26).

As tools de agendamento (S15) entram sempre. `calendars` diz que cliente de calendario
enxerga a agenda de cada profissional; sem ele (`no_calendars`), so profissional com agenda
propria — sem `calendar_id` — aparece na oferta, e os demais saem como "nao consegui
consultar" em vez de "livres" (invariante 1). A ordem e estavel de proposito: a lista de
tools entra no prefixo cacheado do prompt (D-21), e reordenar o invalida.
"""

from __future__ import annotations

from app.agent.tools.base import ToolRegistry
from app.agent.tools.check_availability import check_availability_tool
from app.agent.tools.confirm_appointment import confirm_appointment_tool
from app.agent.tools.escalate_to_human import escalate_to_human_tool
from app.agent.tools.find_appointments import find_appointments_tool
from app.agent.tools.hold_slot import hold_slot_tool
from app.agent.tools.list_services import list_services_tool
from app.agent.tools.save_contact_info import save_contact_info_tool
from app.agent.tools.search_knowledge import search_knowledge_tool
from app.calendar.resolver import no_calendars
from app.core.config import Settings
from app.knowledge.embeddings import EmbeddingProvider
from app.scheduling.availability import CalendarResolver

__all__ = ["build_registry"]


def build_registry(
    *,
    embeddings: EmbeddingProvider | None = None,
    settings: Settings | None = None,
    calendars: CalendarResolver | None = None,
) -> ToolRegistry:
    """Tools disponiveis hoje. Chamado uma vez por processo, no startup."""
    resolver = calendars or no_calendars
    specs = [
        list_services_tool(settings=settings),
        check_availability_tool(resolver, settings=settings),
        hold_slot_tool(settings=settings),
        confirm_appointment_tool(resolver, settings=settings),
        find_appointments_tool(settings=settings),
        save_contact_info_tool(settings=settings),
        escalate_to_human_tool(),
    ]
    if embeddings is not None:
        specs.append(search_knowledge_tool(embeddings, settings=settings))
    return ToolRegistry(tuple(specs))
