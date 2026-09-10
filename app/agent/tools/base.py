"""Contrato das tools do agente (secao 11.4).

Uma tool e quatro coisas: um schema que o modelo ve, um handler que o motor executa, um
tipo (`read` ou `write`) e — se for de escrita — uma **chave de idempotencia** derivada
dos argumentos.

Por que o tipo importa
----------------------
Escrita e leitura falham de formas diferentes. Repetir `check_availability` custa uma
consulta; repetir `confirm_appointment` cria dois agendamentos. O motor so precisa
distinguir os dois casos porque a invariante 5 exige que a segunda chamada com a mesma
chave devolva o resultado da primeira em vez de executar de novo (11.1, regra do passo 7).

Por que `tenant_id` nao esta no schema
--------------------------------------
Porque ele nunca e argumento (invariante 3). O modelo nao tem como pedir para uma tool
olhar o banco de outro cliente — o que ele produz sao os campos de `input_schema`, e o
`tenant_id` chega pelo `ToolContext`, montado pelo worker a partir da conversa.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from app.agent.llm import ToolSchema
from app.domain.tenant_config import TenantConfig

__all__ = [
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "default_idempotency_key",
]

ToolKind = Literal["read", "write"]


class ToolError(Exception):
    """Falha esperada de uma tool, com mensagem que pode voltar ao modelo.

    Excecao inesperada nao vira isto: ela sobe, e o motor a converte num `tool_result`
    de erro generico. Detalhe interno nao vai para o prompt.
    """


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Tudo que a tool pode saber sobre onde ela esta rodando.

    Construido pelo motor, nunca pelo modelo.
    """

    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    contact_id: uuid.UUID
    channel: str
    config: TenantConfig
    now: datetime
    collected: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """O que volta ao modelo. `content` vira JSON no `tool_result`."""

    content: Mapping[str, Any]
    is_error: bool = False
    #: Efeito registrado no `audit_log` — por exemplo o id do agendamento criado.
    entity: str | None = None
    entity_id: uuid.UUID | None = None

    def as_json(self) -> str:
        return json.dumps(self.content, ensure_ascii=False, sort_keys=True, default=str)


class ToolHandler(Protocol):
    async def __call__(self, ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: ToolHandler
    kind: ToolKind = "read"
    #: Modelo Pydantic dos argumentos. Com ele, o motor valida antes de executar e
    #: devolve ao modelo um erro que da para corrigir (passo 7 da 11.1).
    args_model: type[BaseModel] | None = None
    #: Como derivar a chave de idempotencia dos argumentos. `None` usa o hash canonico.
    idempotency_key: Callable[[ToolContext, Mapping[str, Any]], str] | None = None

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name, description=self.description, input_schema=self.input_schema
        )

    def key_for(self, ctx: ToolContext, arguments: Mapping[str, Any]) -> str:
        if self.idempotency_key is not None:
            return self.idempotency_key(ctx, arguments)
        return default_idempotency_key(self.name, arguments)


def default_idempotency_key(name: str, arguments: Mapping[str, Any]) -> str:
    """Hash canonico de nome + argumentos.

    Canonico de verdade: chaves ordenadas e separadores fixos. Sem isso, o mesmo pedido
    com as chaves em outra ordem geraria outra chave — e a protecao contra escrita dupla
    valeria so quando o modelo repetisse o JSON byte a byte.
    """
    payload = json.dumps(
        {"tool": name, "args": arguments}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class ToolRegistry:
    """Conjunto de tools disponiveis num turno.

    A ordem de registro e a ordem enviada ao modelo — e ela e estavel de proposito: a
    lista de tools entra no prefixo cacheado do prompt, e reordenar invalida o cache.
    """

    def __init__(self, specs: tuple[ToolSpec, ...] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool duplicada: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def schemas(self) -> tuple[ToolSchema, ...]:
        return tuple(spec.schema for spec in self._specs.values())

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def __len__(self) -> int:
        return len(self._specs)
