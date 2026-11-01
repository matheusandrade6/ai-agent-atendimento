"""Tool de escrita: cancela um agendamento, com confirmacao em dois turnos (secao 12.3, RF-22).

"Nunca cancela em uma mensagem so" e uma regra do codigo, nao do prompt
-----------------------------------------------------------------------
A tool tem duas chamadas, e a primeira **nao cancela nada**:

1. `cancel_appointment(appointment_id)` confere a politica e devolve
   `confirmation_required` com um `confirmation_token`. O modelo pergunta ao cliente
   "Confirma o cancelamento de X no dia Y?" e o turno acaba.
2. Quando o cliente responde — outro turno —, `cancel_appointment(appointment_id,
   confirmation_token)` cancela. O token (`app.scheduling.confirmation_token`) so existe
   depois da primeira chamada e so vale em turno posterior ao que o emitiu; usado no mesmo
   turno, volta `confirmation_pending`. O modelo nao tem como cancelar na primeira mensagem
   mesmo querendo.

O que o codigo nao le e se a resposta foi "sim" ou "nao": isso e leitura do modelo, e a unica
parte da confirmacao que continua fora do codigo.

Politica
--------
`allow_cancel: false` ou menos antecedencia do que `cancel_min_notice_hours` devolve
`needs_human`, e o motor abre o handoff (D-60). A politica e conferida **nas duas chamadas**:
na primeira, para nao pedir confirmacao de algo que nao sera feito; na segunda, porque entre
as duas o relogio andou e o prazo pode ter fechado.

Calendario
----------
Depois do commit, o evento e apagado (idempotente: ja apagado conta como feito). Falha de
calendario nao desfaz o cancelamento — o cliente pediu e o banco manda (D-57): o
agendamento fica `pending_sync` para o job de reprocessamento, e o evento fantasma e um
incomodo ao profissional, nao uma promessa quebrada ao cliente.

Lista de espera (12.3, passo 4)
-------------------------------
Com `waitlist_enabled`, o horario liberado vai para `on_slot_released`. O consumidor — o job de
encaixe da Fase 3 (RF-25) — ainda nao existe; sem ele registrado, o gancho nao faz nada. A
tool nao conhece fila nem job: so avisa que um horario vagou.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import update

from app.agent.guardrails import redact_pii
from app.agent.tools.base import ToolContext, ToolResult, ToolSpec
from app.agent.tools.common import (
    FIND_APPOINTMENTS,
    AppointmentView,
    appointment_facts,
    confirmation_signer,
    failure,
    load_appointment_view,
    needs_human,
)
from app.core.config import Settings
from app.core.db import tenant_session
from app.core.telemetry import get_logger
from app.domain.models import Appointment
from app.scheduling import booking
from app.scheduling.availability import CalendarResolver
from app.scheduling.confirmation_token import ConfirmationTokenError
from app.scheduling.rules import ChangePolicy

log = get_logger(__name__)

__all__ = [
    "CancelAppointmentArgs",
    "SlotReleaseHook",
    "SlotReleased",
    "cancel_appointment_tool",
]

NAME = "cancel_appointment"

_DESCRIPTION = (
    "Cancela um agendamento, em DOIS turnos. Primeiro ache o agendamento com "
    "`find_appointments` (se houver mais de um, pergunte qual). Depois chame esta tool so com "
    "o `appointment_id`: ela NAO cancela, devolve `confirmation_required` com um "
    "`confirmation_token`. Pergunte ao cliente, com o servico e o dia, se confirma o "
    "cancelamento e ENCERRE o turno. Somente quando o cliente responder que sim, chame de "
    "novo com o mesmo `appointment_id` e o `confirmation_token`, copiado sem alterar; se ele "
    "disser que nao, nao chame mais nada. `reason` e o motivo, se o cliente o disser. Se o "
    "agendamento vier com `can_cancel: false`, chame a tool assim mesmo: ela encaminha a "
    "conversa para a equipe. Resultado `cancelled`: confirme o cancelamento e ofereca "
    "remarcar, uma vez, sem insistir. Resultado `needs_human`: avise que alguem da equipe vai "
    "continuar e nada mais. NUNCA diga que cancelou sem receber `cancelled`."
)

_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "appointment_id": {
            "type": "string",
            "description": "O `appointment_id` do agendamento, de find_appointments.",
        },
        "confirmation_token": {
            "type": "string",
            "description": "O token de `confirmation_required`. Omita na primeira chamada.",
        },
        "reason": {
            "type": "string",
            "description": "Motivo do cancelamento, se o cliente disse. Nao pergunte por isso.",
        },
    },
    "required": ["appointment_id"],
}


class CancelAppointmentArgs(BaseModel):
    appointment_id: uuid.UUID
    confirmation_token: str | None = Field(default=None, max_length=1000)
    reason: str | None = Field(default=None, max_length=200)

    @field_validator("confirmation_token", "reason")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        stripped = (value or "").strip()
        return stripped or None


@dataclass(frozen=True, slots=True)
class SlotReleased:
    """Um horario que vagou por cancelamento. O que o job de encaixe precisa para procurar."""

    tenant_id: uuid.UUID
    provider_id: uuid.UUID
    resource_id: uuid.UUID | None
    service_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime


SlotReleaseHook = Callable[[SlotReleased], Awaitable[None]]

# ------------------------------------ respostas ------------------------------------

_BLOCKED: Final[Mapping[str, tuple[str, str]]] = {
    "too_late": (
        "cancelamento_fora_do_prazo",
        "Faltam menos horas para o atendimento do que a politica da casa exige para cancelar "
        "por aqui.",
    ),
    "not_allowed": (
        "cancelamento_nao_permitido",
        "A casa nao cancela por conversa: so a equipe faz isso.",
    ),
}

_NOT_FOUND: Final = (
    "appointment_not_found",
    "Nao existe esse agendamento para este cliente.",
    FIND_APPOINTMENTS,
)
_NOT_ACTIVE: Final = (
    "appointment_not_active",
    "Esse agendamento nao esta mais ativo: ja foi cancelado ou remarcado.",
    FIND_APPOINTMENTS,
)

_ASK_AGAIN: Final = "call cancel_appointment with only the appointment_id to ask for confirmation"


@dataclass(frozen=True, slots=True)
class _Handler:
    calendars: CalendarResolver
    settings: Settings | None
    on_slot_released: SlotReleaseHook | None

    async def __call__(self, ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        args = CancelAppointmentArgs.model_validate(arguments)
        policy = ChangePolicy.from_rules(ctx.config.scheduling)

        if args.confirmation_token is None:
            return await self._ask(ctx, args, policy)

        try:
            confirmation_signer(self.settings).verify(
                args.confirmation_token,
                appointment_id=args.appointment_id,
                tenant_id=ctx.tenant_id,
                conversation_id=ctx.conversation_id,
                now=ctx.now,
            )
        except ConfirmationTokenError as exc:
            log.warning("confirmation_token_recusado", motivo=exc.reason)
            if exc.reason == "same_turn":
                return failure(
                    "confirmation_pending",
                    "O cancelamento so vale depois que o cliente responder. Pergunte e "
                    "encerre o turno.",
                    "ask the customer to confirm, end the turn and wait for the answer",
                )
            return failure(
                "invalid_confirmation_token",
                "Esse pedido de confirmacao nao vale mais.",
                _ASK_AGAIN,
            )
        return await self._cancel(ctx, args, policy)

    # ------------------------------ 1a chamada: pergunta ------------------------------

    async def _ask(
        self, ctx: ToolContext, args: CancelAppointmentArgs, policy: ChangePolicy
    ) -> ToolResult:
        async with tenant_session(ctx.tenant_id, self.settings) as session:
            verdict = await booking.check_change(
                session,
                tenant_id=ctx.tenant_id,
                appointment_id=args.appointment_id,
                contact_id=ctx.contact_id,
                kind="cancel",
                policy=policy,
                now=ctx.now,
            )
            view = (
                await load_appointment_view(session, ctx, args.appointment_id)
                if verdict == "ok"
                else None
            )
        refusal = self._refusal(verdict)
        if refusal is not None:
            return refusal
        if view is None:
            return failure(*_NOT_FOUND)

        token = confirmation_signer(self.settings).sign(
            args.appointment_id,
            tenant_id=ctx.tenant_id,
            conversation_id=ctx.conversation_id,
            now=ctx.now,
        )
        return ToolResult(
            content={
                "status": "confirmation_required",
                **appointment_facts(view),
                "confirmation_token": token,
                "message": "Nada foi cancelado. Pergunte ao cliente se confirma o cancelamento "
                "deste agendamento e encerre o turno.",
            },
            entity="appointment",
            entity_id=view.appointment.id,
        )

    # --------------------------- 2a chamada: cancela de fato ---------------------------

    async def _cancel(
        self, ctx: ToolContext, args: CancelAppointmentArgs, policy: ChangePolicy
    ) -> ToolResult:
        # O motivo e texto livre da pessoa e vai para o banco e para o painel: documento e
        # cartao saem antes (18.2). Nao recusa — o cancelamento nao pode depender do motivo.
        reason = redact_pii(args.reason)[0] if args.reason else None
        async with tenant_session(ctx.tenant_id, self.settings) as session:
            result = await booking.cancel_appointment(
                session,
                tenant_id=ctx.tenant_id,
                appointment_id=args.appointment_id,
                contact_id=ctx.contact_id,
                conversation_id=ctx.conversation_id,
                policy=policy,
                reason=reason,
                now=ctx.now,
            )
            view = (
                await load_appointment_view(session, ctx, args.appointment_id)
                if result.status == "cancelled"
                else None
            )
        refusal = self._refusal(result.status)
        if refusal is not None:
            return refusal
        if view is None:
            return failure(*_NOT_FOUND)

        await self._remove_event(ctx, view)
        if ctx.config.scheduling.waitlist_enabled and not result.replayed:
            await self._release(ctx, view)
        return ToolResult(
            content={
                "status": "cancelled",
                **appointment_facts(view),
                "message": "Cancelado. Confirme ao cliente e ofereca remarcar, uma vez, sem "
                "insistir.",
            },
            entity="appointment",
            entity_id=view.appointment.id,
        )

    @staticmethod
    def _refusal(status: str) -> ToolResult | None:
        if status in _BLOCKED:
            return needs_human(*_BLOCKED[status])
        if status == "not_found":
            return failure(*_NOT_FOUND)
        if status == "not_active":
            return failure(*_NOT_ACTIVE)
        return None

    # --------------------------------- depois do commit ---------------------------------

    async def _remove_event(self, ctx: ToolContext, view: AppointmentView) -> None:
        """Apaga o evento. Nunca levanta: o cancelamento ja esta no banco."""
        appointment = view.appointment
        event_id = appointment.external_event_id
        calendar_id = view.provider.calendar_id
        if event_id is None or calendar_id is None:
            return
        status = "synced"
        try:
            client = self.calendars(view.provider)
            if client is None:
                log.error("calendar_sem_credencial", appointment_id=str(appointment.id))
                status = "sync_failed"
            else:
                await client.delete_event(calendar_id, event_id)
        except Exception:
            log.exception("cancelamento_evento_nao_removido", appointment_id=str(appointment.id))
            status = "pending_sync"

        try:
            async with tenant_session(ctx.tenant_id, self.settings) as session:
                values: dict[str, Any] = {"sync_status": status}
                if status == "synced":
                    values["external_event_id"] = None
                await session.execute(
                    update(Appointment)
                    .where(Appointment.tenant_id == ctx.tenant_id, Appointment.id == appointment.id)
                    .values(**values)
                )
        except Exception:
            log.exception("cancelamento_sync_nao_gravado", appointment_id=str(appointment.id))

    async def _release(self, ctx: ToolContext, view: AppointmentView) -> None:
        """Avisa o job de encaixe de que o horario vagou. Nunca levanta."""
        if self.on_slot_released is None:
            return
        appointment = view.appointment
        try:
            await self.on_slot_released(
                SlotReleased(
                    tenant_id=ctx.tenant_id,
                    provider_id=appointment.provider_id,
                    resource_id=appointment.resource_id,
                    service_id=appointment.service_id,
                    starts_at=appointment.starts_at,
                    ends_at=appointment.ends_at,
                )
            )
        except Exception:
            log.exception("encaixe_nao_disparado", appointment_id=str(appointment.id))


def cancel_appointment_tool(
    calendars: CalendarResolver,
    *,
    settings: Settings | None = None,
    on_slot_released: SlotReleaseHook | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema=_INPUT_SCHEMA,
        handler=_Handler(calendars=calendars, settings=settings, on_slot_released=on_slot_released),
        kind="write",
        args_model=CancelAppointmentArgs,
    )
