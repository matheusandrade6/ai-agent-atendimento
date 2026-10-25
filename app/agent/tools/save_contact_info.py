"""Tool de escrita: grava o que a pessoa disse sobre si e sobre quem sera atendido (11.4, 11.7).

O que ela escreve
-----------------
No **contato da conversa** — nome, email e `attributes` — e, se vier `subject`, em quem sera
atendido (o proprio contato, um dependente ou um pet). O contato e o do contexto, nunca um
argumento: o modelo nao escolhe de quem grava (invariante 3, no espirito).

Reescrever, nao acumular: o mesmo `subject` (mesmo tipo e nome, sem diferenciar caixa) e
atualizado, nao duplicado, entao repetir a chamada e inofensivo (invariante 5). `attributes`
e mesclado chave a chave — um turno que traz so a especie nao apaga a raca ja gravada.

O que ela recusa
----------------
Documento, CNPJ e cartao (18.2: o agente nao coleta nem guarda). A deteccao e a mesma do
guardrail de saida (`app.agent.guardrails.redact_pii`): quem manda o numero espontaneamente
nao o ve persistido, e o modelo recebe o motivo e segue a conversa sem ele. Os `attributes`
tambem sao fechados na forma — chave curta em minusculas, valor escalar de ate 200 caracteres
—, porque este e o unico lugar onde o modelo escreve JSON livre no banco.

`collected` (campos do intake da conversa) nao e tocado aqui: ele pertence ao motor, que o
recalcula a cada turno e o grava em `persist_turn`; uma escrita daqui seria sobrescrita.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.guardrails import redact_pii
from app.agent.tools.base import ToolContext, ToolError, ToolResult, ToolSpec
from app.agent.tools.common import failure
from app.core.config import Settings
from app.core.db import tenant_session
from app.core.telemetry import get_logger
from app.domain.models import Contact, Subject

log = get_logger(__name__)

__all__ = ["SaveContactInfoArgs", "save_contact_info_tool"]

NAME = "save_contact_info"

SubjectKind = Literal["self", "dependent", "pet"]
Scalar = str | int | float | bool

_ATTRIBUTE_KEY: Final = re.compile(r"[a-z][a-z0-9_]{0,39}")
_EMAIL: Final = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_MAX_ATTRIBUTES: Final = 20
_MAX_VALUE_CHARS: Final = 200

_DESCRIPTION = (
    "Grava o que o cliente disse sobre si ou sobre quem sera atendido: o nome dele "
    "(`contact_name`), o email, e — quando o atendimento e para um pet, um dependente ou o "
    "proprio cliente — o `subject` com o tipo (`kind`), o nome e atributos como especie ou "
    "raca. Use assim que a informacao aparecer; repetir e inofensivo, o registro e "
    "atualizado. Grave so o que o cliente falou: nao deduza, nao complete. Nunca grave "
    "CPF, RG, cartao ou dado bancario — a ferramenta recusa."
)

_ATTRIBUTES_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "description": "Chave (minusculas, ex.: especie) -> valor simples (texto, numero ou "
    "verdadeiro/falso).",
}

_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "contact_name": {"type": "string", "description": "Nome de quem esta conversando."},
        "email": {"type": "string", "description": "Email do cliente, se ele informou."},
        "subject": {
            "type": "object",
            "description": "Quem sera atendido.",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["self", "dependent", "pet"],
                    "description": "self = o proprio cliente; dependent = outra pessoa; pet.",
                },
                "name": {"type": "string", "description": "Nome de quem sera atendido."},
                "attributes": _ATTRIBUTES_SCHEMA,
            },
            "required": ["kind", "name"],
        },
        "attributes": _ATTRIBUTES_SCHEMA,
    },
    "required": [],
}


def _checked_attributes(value: dict[str, Scalar]) -> dict[str, Scalar]:
    if len(value) > _MAX_ATTRIBUTES:
        raise ValueError(f"mais de {_MAX_ATTRIBUTES} atributos")
    for key, item in value.items():
        if not _ATTRIBUTE_KEY.fullmatch(key):
            raise ValueError(f"chave de atributo invalida: {key!r}")
        if isinstance(item, str) and len(item) > _MAX_VALUE_CHARS:
            raise ValueError(f"valor de `{key}` com mais de {_MAX_VALUE_CHARS} caracteres")
    return value


class SubjectInfo(BaseModel):
    kind: SubjectKind
    name: str = Field(min_length=1, max_length=120)
    attributes: dict[str, Scalar] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("nome vazio")
        return stripped

    @field_validator("attributes")
    @classmethod
    def _attributes(cls, value: dict[str, Scalar]) -> dict[str, Scalar]:
        return _checked_attributes(value)


class SaveContactInfoArgs(BaseModel):
    contact_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    subject: SubjectInfo | None = None
    attributes: dict[str, Scalar] = Field(default_factory=dict)

    @field_validator("contact_name", "email")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        stripped = (value or "").strip()
        return stripped or None

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value: str | None) -> str | None:
        if value is not None and not _EMAIL.fullmatch(value):
            raise ValueError("email invalido")
        return value

    @field_validator("attributes")
    @classmethod
    def _attributes(cls, value: dict[str, Scalar]) -> dict[str, Scalar]:
        return _checked_attributes(value)

    @model_validator(mode="after")
    def _something_to_save(self) -> Self:
        if not (self.contact_name or self.email or self.subject or self.attributes):
            raise ValueError("nada para salvar")
        return self

    def texts(self) -> Iterator[str]:
        """Todo texto livre que vai ser gravado — o que a conferencia de PII precisa ler."""
        for text in (self.contact_name, self.email):
            if text:
                yield text
        sources: list[Mapping[str, Scalar]] = [self.attributes]
        if self.subject:
            yield self.subject.name
            sources.append(self.subject.attributes)
        for attributes in sources:
            yield from (value for value in attributes.values() if isinstance(value, str))


def _sensitive_kinds(args: SaveContactInfoArgs) -> tuple[str, ...]:
    found: list[str] = []
    for text in args.texts():
        found.extend(redact_pii(text)[1])
    return tuple(dict.fromkeys(found))


@dataclass(frozen=True, slots=True)
class _Handler:
    settings: Settings | None

    async def __call__(self, ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        args = SaveContactInfoArgs.model_validate(arguments)

        sensitive = _sensitive_kinds(args)
        if sensitive:
            # So o tipo vai para o log; o valor nunca (18.2).
            log.warning("save_contact_info_recusou_pii", tipos=list(sensitive))
            return failure(
                "sensitive_data",
                "Nao posso guardar documento, cartao ou dado bancario. Nada foi salvo.",
                "continue the conversation without that data; do not ask for it again",
            )

        saved: list[str] = []
        subject_payload: dict[str, str] | None = None
        async with tenant_session(ctx.tenant_id, self.settings) as session:
            contact = (
                await session.execute(
                    select(Contact)
                    .where(Contact.tenant_id == ctx.tenant_id, Contact.id == ctx.contact_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if contact is None:
                raise ToolError("contato da conversa nao encontrado")

            if args.contact_name:
                contact.name = args.contact_name
                saved.append("contact_name")
            if args.email:
                contact.email = args.email
                saved.append("email")
            if args.attributes:
                contact.attributes = {**contact.attributes, **args.attributes}
                saved.append("attributes")

            if args.subject:
                subject_id = await self._save_subject(session, ctx, args.subject)
                subject_payload = {
                    "subject_id": str(subject_id),
                    "kind": args.subject.kind,
                    "name": args.subject.name,
                }
                saved.append("subject")

        content: dict[str, Any] = {"status": "saved", "saved": saved}
        if subject_payload:
            content["subject"] = subject_payload
        return ToolResult(content=content, entity="contact", entity_id=ctx.contact_id)

    @staticmethod
    async def _save_subject(
        session: AsyncSession, ctx: ToolContext, info: SubjectInfo
    ) -> uuid.UUID:
        """Mescla atributos no subject do contato com mesmo tipo e nome, ou cria um.

        O nome gravado na primeira vez nao muda: "mia" digitado depois nao renomeia "Mia".
        """
        rows = (
            await session.execute(
                select(Subject).where(
                    Subject.tenant_id == ctx.tenant_id,
                    Subject.contact_id == ctx.contact_id,
                    Subject.kind == info.kind,
                )
            )
        ).scalars()
        wanted = info.name.casefold()
        for subject in rows:
            if (subject.name or "").strip().casefold() == wanted:
                subject.attributes = {**subject.attributes, **info.attributes}
                found: uuid.UUID = subject.id
                return found
        created = await session.scalar(
            insert(Subject)
            .values(
                tenant_id=ctx.tenant_id,
                contact_id=ctx.contact_id,
                kind=info.kind,
                name=info.name,
                attributes=dict(info.attributes),
            )
            .returning(Subject.id)
        )
        assert created is not None
        return created


def save_contact_info_tool(*, settings: Settings | None = None) -> ToolSpec:
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema=_INPUT_SCHEMA,
        handler=_Handler(settings=settings),
        kind="write",
        args_model=SaveContactInfoArgs,
    )
