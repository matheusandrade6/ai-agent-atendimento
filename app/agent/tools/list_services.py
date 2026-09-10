"""Tool de leitura: catalogo de servicos do tenant (secao 11.4).

Casamento por `services.aliases`
---------------------------------
O cliente pede o servico como fala no dia a dia — "tosa", "banho e tosa", "consulta de
rotina" — nao como ele esta cadastrado. `services.aliases` (S02) guarda esses sinonimos
por servico; o filtro textual aqui casa contra o nome e contra cada apelido, ignorando
caixa e acento. A comparacao vale nos dois sentidos (filtro dentro do nome/apelido, ou
nome/apelido dentro do filtro) porque o modelo tanto manda um termo curto quanto repete
um trecho da frase da pessoa.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from app.agent.tools.base import ToolContext, ToolResult, ToolSpec
from app.core.config import Settings
from app.core.db import tenant_session
from app.domain.models import Service

__all__ = ["ListServicesArgs", "list_services_tool"]

NAME = "list_services"

#: Abaixo disso, so aceitamos o filtro "contido no" nome/apelido — nunca o inverso — para
#: um filtro de uma letra nao casar com o catalogo inteiro.
_MIN_REVERSE_MATCH_LEN = 3

_DESCRIPTION = (
    "Catalogo de servicos do estabelecimento, com duracao, preco e modalidade. Aceita "
    "um filtro textual opcional que casa com o nome do servico ou com um sinonimo "
    "configurado (por exemplo, 'tosa' encontra 'Banho e tosa'). Sem filtro, devolve o "
    "catalogo inteiro."
)

_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Filtro textual opcional: nome do servico ou sinonimo.",
        }
    },
    "required": [],
}


class ListServicesArgs(BaseModel):
    query: str | None = None


def _normalize(text: str) -> str:
    """Sem acento e sem caixa: 'Tosa', 'tosa' e 'tosà' casam entre si."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _matches(service: Service, query_norm: str) -> bool:
    if not query_norm:
        return True
    candidates = [service.name, *service.aliases]
    for candidate in candidates:
        candidate_norm = _normalize(candidate)
        if query_norm in candidate_norm:
            return True
        if len(candidate_norm) >= _MIN_REVERSE_MATCH_LEN and candidate_norm in query_norm:
            return True
    return False


def _price_line(service: Service) -> str:
    if service.price_cents is not None:
        reais = service.price_cents / 100
        return f"R$ {reais:.2f}".replace(".", ",")
    return service.price_note or ""


def _service_dict(service: Service) -> dict[str, Any]:
    return {
        "id": str(service.id),
        "name": service.name,
        "duration_minutes": service.duration_minutes,
        "price_line": _price_line(service),
        "modality": service.modality,
        "description": service.description or "",
    }


@dataclass(frozen=True, slots=True)
class _Handler:
    settings: Settings | None

    async def __call__(self, ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        raw_query = arguments.get("query")
        query_norm = _normalize(str(raw_query)) if raw_query else ""

        statement = (
            select(Service)
            .where(Service.tenant_id == ctx.tenant_id, Service.active.is_(True))
            .order_by(Service.name)
        )
        async with tenant_session(ctx.tenant_id, self.settings) as session:
            services: Sequence[Service] = (await session.scalars(statement)).all()

        matched = [service for service in services if _matches(service, query_norm)]
        return ToolResult(content={"services": [_service_dict(service) for service in matched]})


def list_services_tool(*, settings: Settings | None = None) -> ToolSpec:
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema=_INPUT_SCHEMA,
        handler=_Handler(settings=settings),
        kind="read",
        args_model=ListServicesArgs,
    )
