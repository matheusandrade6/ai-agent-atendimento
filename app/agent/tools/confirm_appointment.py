"""Tool de escrita: efetiva o agendamento e o espelha no calendario (secao 11.4, RF-19, RF-21).

Duas fases, e a ordem e o ponto
-------------------------------
1. **Banco, numa transacao.** `app.scheduling.booking.confirm_appointment` transforma o hold
   em agendamento sob o advisory lock; a transacao fecha (commit) antes de qualquer rede.
2. **Calendario, depois do commit** (D-43, D-51). O evento e criado com
   `push_appointment_event`, fora do lock — uma retentativa de dezenas de segundos de um
   terceiro nao pode segurar o profissional daquele dia.

O agendamento nasce `pending_sync`. Falha do calendario nao desfaz nada: o banco e a fonte
de verdade, o evento e o espelho, e quem o reconcilia depois e o job de `pending_sync`
(S19/S20). Por isso a tool devolve `confirmed` mesmo com o calendario fora do ar — dizer
"nao consegui agendar" para uma reserva que existe faria o cliente tentar de novo e, pior,
chegar a outra clinica. Qualquer excecao inesperada da fase 2 e engolida pelo mesmo motivo.

Fase 2 so roda quando falta sincronizar: repetir a confirmacao de um agendamento ja
`synced` nao toca o calendario.

O que volta ao modelo
---------------------
Fatos, mais `confirmation_message` quando o tenant configurou `messages.confirmation`: o
texto preenchido pelo mesmo mecanismo dos demais templates (`app.domain.templates`, D-38).
Placeholder sem valor — preco que o servico nao tem, preparo vazio, endereco de servico
online — sai com a frase inteira, nao como `{prep_line}` literal.

Falhas recuperaveis (`hold_expired`, `slot_taken`, `hold_not_found`) voltam como erro com
`suggested_action`, no formato da 11.4.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Self

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.memory import merge_collected
from app.agent.tools.base import ToolContext, ToolResult, ToolSpec
from app.agent.tools.common import (
    RECHECK_AVAILABILITY,
    ProviderLabel,
    failure,
    human_date,
    human_slot,
    human_time,
    price_line,
    provider_labels,
)
from app.calendar.base import EventDraft
from app.calendar.sync_state import push_appointment_event
from app.core.config import Settings
from app.core.db import tenant_session
from app.core.telemetry import get_logger
from app.domain.models import Appointment, Contact, Provider, Service, Subject
from app.domain.templates import drop_unresolved, fill
from app.scheduling.availability import CalendarResolver
from app.scheduling.booking import ConfirmResult
from app.scheduling.booking import confirm_appointment as book_appointment

log = get_logger(__name__)

__all__ = ["ConfirmAppointmentArgs", "confirm_appointment_tool"]

NAME = "confirm_appointment"

#: Onde `notes` mora: `appointments` nao tem coluna para observacao, e o `intake` e o JSON
#: que o painel ja mostra. O underscore o separa das chaves que o tenant declara.
NOTES_KEY: Final = "_notes"

_MAX_INTAKE_KEYS: Final = 40
_MAX_INTAKE_BYTES: Final = 4000

#: Campos do agendamento que podem legitimamente nao existir: servico sem preco, sem preparo,
#: ou online (sem endereco). Vazios, nao quebram a mensagem — o resto continua verdadeiro.
_OPTIONAL_FIELDS: Final = ("price_line", "prep_line", "address")

_DESCRIPTION = (
    "Efetiva o agendamento a partir da reserva (`hold_id`, de `hold_slot`). So chame depois "
    "que o cliente escolheu explicitamente um horario e todos os campos obrigatorios foram "
    "coletados. Se o atendimento e para um pet ou dependente, chame `save_contact_info` com "
    "o `subject` antes. `intake` leva os campos coletados (chave -> valor); `notes`, "
    "observacoes livres do cliente. Em caso de sucesso (`status: confirmed`), confirme ao "
    "cliente usando os dados devolvidos — se vier `confirmation_message`, envie esse texto. "
    "Se o resultado for `hold_expired` ou `slot_taken`, o horario nao esta mais garantido: "
    "NAO diga que agendou; chame `check_availability` de novo."
)

_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "hold_id": {"type": "string", "description": "O `hold_id` devolvido por hold_slot."},
        "subject_name": {
            "type": "string",
            "description": "Nome de quem sera atendido (pet, dependente ou o proprio cliente).",
        },
        "intake": {
            "type": "object",
            "description": "Campos coletados, chave -> valor. So chaves do intake do "
            "estabelecimento sao guardadas.",
        },
        "notes": {"type": "string", "description": "Observacoes livres do cliente."},
    },
    "required": ["hold_id"],
}


class ConfirmAppointmentArgs(BaseModel):
    hold_id: uuid.UUID
    subject_name: str | None = Field(default=None, max_length=120)
    intake: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("subject_name", "notes")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        stripped = (value or "").strip()
        return stripped or None

    @model_validator(mode="after")
    def _bounded_intake(self) -> Self:
        if len(self.intake) > _MAX_INTAKE_KEYS:
            raise ValueError(f"intake com mais de {_MAX_INTAKE_KEYS} campos")
        if len(json.dumps(self.intake, default=str)) > _MAX_INTAKE_BYTES:
            raise ValueError("intake grande demais")
        return self


# ------------------------------------ resultado ------------------------------------

_FAILURES: Final[Mapping[str, tuple[str, str]]] = {
    "hold_expired": ("O horário não está mais reservado.", RECHECK_AVAILABILITY),
    "slot_taken": ("Esse horario acabou de ser ocupado por outra pessoa.", RECHECK_AVAILABILITY),
    "hold_not_found": (
        "Nao existe reserva com esse hold_id nesta conversa.",
        "call hold_slot with a slot_token from check_availability",
    ),
}


@dataclass(frozen=True, slots=True)
class _Booked:
    """O que a fase 2 e a resposta precisam saber do agendamento recem-confirmado."""

    appointment_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    sync_status: str
    service: Service
    provider: Provider
    contact_name: str | None
    contact_phone: str | None
    subject_name: str | None
    notes: str | None


async def _resolve_subject(
    session: AsyncSession, ctx: ToolContext, name: str | None
) -> uuid.UUID | None:
    """O `subject` do contato com esse nome, se houver exatamente um.

    Nao cria: tipo do subject (pet, dependente) e fato que so `save_contact_info` recebe, e
    inventar um aqui gravaria dado errado. Sem subject, o nome continua no agendamento (no
    `intake` e no `confirmation_message`). Dois com o mesmo nome e ambiguidade, nao palpite.
    """
    if not name:
        return None
    rows = (
        await session.execute(
            select(Subject.id, Subject.name).where(
                Subject.tenant_id == ctx.tenant_id, Subject.contact_id == ctx.contact_id
            )
        )
    ).all()
    wanted = name.casefold()
    matches = [row.id for row in rows if (row.name or "").strip().casefold() == wanted]
    return matches[0] if len(matches) == 1 else None


async def _load(
    session: AsyncSession, ctx: ToolContext, appointment_id: uuid.UUID
) -> _Booked | None:
    row = (
        await session.execute(
            select(Appointment, Service, Provider, Contact)
            .join(Service, Service.id == Appointment.service_id)
            .join(Provider, Provider.id == Appointment.provider_id)
            .join(Contact, Contact.id == Appointment.contact_id)
            .where(Appointment.tenant_id == ctx.tenant_id, Appointment.id == appointment_id)
        )
    ).first()
    if row is None:
        return None
    appointment, service, provider, contact = row
    subject_name: str | None = None
    if appointment.subject_id is not None:
        subject_name = await session.scalar(
            select(Subject.name).where(
                Subject.tenant_id == ctx.tenant_id, Subject.id == appointment.subject_id
            )
        )
    return _Booked(
        appointment_id=appointment.id,
        starts_at=appointment.starts_at,
        ends_at=appointment.ends_at,
        sync_status=appointment.sync_status,
        service=service,
        provider=provider,
        contact_name=contact.name,
        contact_phone=contact.phone_e164,
        subject_name=subject_name or appointment.intake.get("subject_name"),
        notes=appointment.intake.get(NOTES_KEY),
    )


def _render_confirmation(template: str, values: Mapping[str, str], *, essentials: list[str]) -> str:
    """Preenche `messages.confirmation` sem nunca perder o quando.

    O caminho normal e o de `app.domain.templates` (D-38): placeholder sem valor leva a
    linha — ou a frase — junto. Num template escrito com um campo por linha, e o que se quer:
    servico sem preco perde a linha do preco e mais nada.

    Mas um YAML com `>` dobra o template numa linha so, e a "frase" passa a ser o texto
    inteiro: um `{prep_line}` vazio apagaria a confirmacao e sobraria "Prontinho!", sem data
    nem hora. Entao, se o resultado perdeu o que o cliente mais precisa ler (`essentials`),
    refaz apagando **so** os campos opcionais vazios (preco, preparo, endereco).
    """
    rendered = drop_unresolved(fill(template, values))
    if all(item in rendered for item in essentials):
        return rendered

    blanked = {name: "" for name in _OPTIONAL_FIELDS if name not in values}
    relaxed = fill(template, {**values, **blanked})
    relaxed = re.sub(r"[ \t]+([.,;:!?])", r"\1", relaxed)
    relaxed = re.sub(r"[ \t]{2,}", " ", relaxed)
    relaxed = re.sub(r"\n{3,}", "\n\n", relaxed)
    return drop_unresolved(relaxed.strip())


def _event_draft(ctx: ToolContext, booked: _Booked, label: ProviderLabel) -> EventDraft:
    who = booked.subject_name or booked.contact_name
    lines = []
    if booked.contact_name or booked.contact_phone:
        contact = " ".join(part for part in (booked.contact_name, booked.contact_phone) if part)
        lines.append(f"Contato: {contact}")
    if booked.subject_name:
        lines.append(f"Atendido: {booked.subject_name}")
    if booked.notes:
        lines.append(f"Observações: {booked.notes}")
    lines.append("Agendado pelo assistente virtual.")
    return EventDraft(
        summary=f"{booked.service.name} - {who}" if who else booked.service.name,
        start=booked.starts_at,
        end=booked.ends_at,
        timezone=label.timezone,
        description="\n".join(lines),
        tenant_ref=str(ctx.tenant_id),
    )


@dataclass(frozen=True, slots=True)
class _Handler:
    calendars: CalendarResolver
    settings: Settings | None

    async def __call__(self, ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        args = ConfirmAppointmentArgs.model_validate(arguments)

        # So chaves que o intake do tenant declara entram (o modelo nao inventa campo), e o
        # que o motor ja coletou nao e apagado por um turno sem informacao nova.
        volunteered = dict(args.intake)
        if args.subject_name:
            volunteered.setdefault("subject_name", args.subject_name)
        intake = merge_collected(ctx.config.intake, ctx.collected, volunteered)
        if args.notes:
            intake[NOTES_KEY] = args.notes

        async with tenant_session(ctx.tenant_id, self.settings) as session:
            subject_id = await _resolve_subject(session, ctx, args.subject_name)
            result = await book_appointment(
                session,
                tenant_id=ctx.tenant_id,
                hold_id=args.hold_id,
                now=ctx.now,
                subject_id=subject_id,
                intake=intake,
                conversation_id=ctx.conversation_id,
            )
            booked = (
                await _load(session, ctx, result.appointment_id)
                if result.status == "confirmed" and result.appointment_id is not None
                else None
            )
            labels = (
                await provider_labels(
                    session,
                    ctx.tenant_id,
                    {booked.provider.id},
                    default_timezone=ctx.config.business_hours.timezone,
                )
                if booked
                else {}
            )

        if booked is None:
            return self._refusal(result)

        label = labels.get(booked.provider.id) or ProviderLabel(
            name=booked.provider.display_name or booked.provider.name,
            timezone=ctx.config.business_hours.timezone,
        )
        if booked.sync_status != "synced":
            await self._mirror(ctx, booked, label)
        return self._success(ctx, booked, label)

    # ------------------------------ fase 2: calendario ------------------------------

    async def _mirror(self, ctx: ToolContext, booked: _Booked, label: ProviderLabel) -> None:
        """Cria o evento e grava o desfecho. Nunca levanta: o agendamento ja existe."""
        try:
            provider = booked.provider
            if provider.calendar_id is None:
                # Agenda propria: nao ha espelho a manter, entao nao ha o que sincronizar.
                await self._set_sync(ctx, booked.appointment_id, "synced")
                return
            client = self.calendars(provider)
            if client is None:
                log.error(
                    "calendar_sem_credencial",
                    appointment_id=str(booked.appointment_id),
                    provider_id=str(provider.id),
                )
                await self._set_sync(ctx, booked.appointment_id, "sync_failed")
                return
            async with tenant_session(ctx.tenant_id, self.settings) as session:
                await push_appointment_event(
                    session,
                    client,
                    appointment_id=booked.appointment_id,
                    calendar_id=provider.calendar_id,
                    draft=_event_draft(ctx, booked, label),
                )
        except Exception:
            log.exception("espelho_no_calendario_falhou", appointment_id=str(booked.appointment_id))

    async def _set_sync(self, ctx: ToolContext, appointment_id: uuid.UUID, status: str) -> None:
        async with tenant_session(ctx.tenant_id, self.settings) as session:
            await session.execute(
                update(Appointment)
                .where(Appointment.tenant_id == ctx.tenant_id, Appointment.id == appointment_id)
                .values(sync_status=status)
            )

    # --------------------------------- resposta ---------------------------------

    @staticmethod
    def _refusal(result: ConfirmResult) -> ToolResult:
        # `confirmed` sem agendamento legivel nao deveria existir; vira o erro mais honesto
        # que ha — a reserva nao se sustenta — em vez de um `confirmed` sem dados.
        status = result.status if result.status in _FAILURES else "hold_not_found"
        message, action = _FAILURES[status]
        return failure(status, message, action)

    @staticmethod
    def _success(ctx: ToolContext, booked: _Booked, label: ProviderLabel) -> ToolResult:
        local = label.local(booked.starts_at)
        service = booked.service
        online = service.modality == "online"
        subject = booked.subject_name or booked.contact_name or ""
        appointment_values = {
            "service": service.name,
            "subject_name": subject,
            "provider": label.name,
            "date_human": human_date(local),
            "time": human_time(local),
            "price_line": price_line(service),
            "prep_line": service.prep_instructions or "",
        }
        address = "" if online else ctx.config.template_values().get("address", "")

        content: dict[str, Any] = {
            "status": "confirmed",
            "appointment_id": str(booked.appointment_id),
            "service": service.name,
            "subject_name": subject,
            "starts_at": local.isoformat(),
            "human": human_slot(local),
            "provider_name": label.name,
            "address": address,
            "price_line": appointment_values["price_line"],
            "prep_line": appointment_values["prep_line"],
        }
        template = ctx.config.messages.confirmation.strip()
        if template:
            values = ctx.config.template_values()
            values.pop("address", None)
            if booked.contact_name:
                values["contact_name"] = booked.contact_name
            if address:
                values["address"] = address
            # Campo vazio fica fora do mapa: o placeholder sem valor leva a frase junto.
            values.update({key: value for key, value in appointment_values.items() if value})
            content["confirmation_message"] = _render_confirmation(
                template,
                values,
                essentials=[
                    appointment_values[key]
                    for key in ("date_human", "time")
                    if f"{{{key}}}" in template
                ],
            )
        return ToolResult(content=content, entity="appointment", entity_id=booked.appointment_id)


def _idempotency_key(ctx: ToolContext, arguments: Mapping[str, Any]) -> str:
    """Uma confirmacao por reserva: observacao ou campo diferente nao a repete (invariante 5)."""
    return f"{NAME}:{arguments['hold_id']}"


def confirm_appointment_tool(
    calendars: CalendarResolver, *, settings: Settings | None = None
) -> ToolSpec:
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema=_INPUT_SCHEMA,
        handler=_Handler(calendars=calendars, settings=settings),
        kind="write",
        args_model=ConfirmAppointmentArgs,
        idempotency_key=_idempotency_key,
    )
