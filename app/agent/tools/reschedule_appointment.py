"""Tool de escrita: move um agendamento para outro horario ofertado (secao 12.2, RF-22).

Duas fases, como a confirmacao — e a ordem e o ponto
----------------------------------------------------
1. **Banco, numa transacao.** `app.scheduling.booking.reschedule_appointment` confere a
   politica de antecedencia com a linha travada, cria o agendamento novo e cancela o antigo
   num savepoint: os dois ou nenhum. A transacao fecha antes de qualquer rede.
2. **Calendario, depois do commit** (D-43). O evento do profissional passa para o horario
   novo — o mesmo evento, quando e o mesmo calendario; criado e apagado, quando muda de
   profissional.

O que a 12.2 chama de "rollback" e o que acontece quando a fase 2 falha
-----------------------------------------------------------------------
Na confirmacao (D-57) falha de calendario nao desfaz nada: o agendamento e novo e o evento
ainda nao existe, entao o pior caso e "sem evento". Aqui e diferente. O evento **ja existe**
no horario antigo; se ele nao anda, o profissional olha a agenda e espera o cliente na hora
errada. Por isso, **quando havia evento e ele nao pode ser movido**, a remarcacao e desfeita
(`booking.revert_reschedule`) e a conversa escala: o cliente continua com o horario que tinha.
Sem evento anterior (agenda propria, sincronizacao pendente), a falha de calendario degrada
como na confirmacao — `pending_sync`, sem desfazer.

Escalada por regra, nao por pedido do modelo
--------------------------------------------
Abaixo da antecedencia minima, com `allow_reschedule: false` ou numa falha com rollback, a
tool devolve `needs_human` e o motor abre o handoff no mesmo turno (D-60). O modelo so redige
o aviso. `slot_taken`, em contraste, e corrida normal e volta como erro recuperavel: o
agendamento antigo segue valendo e o modelo reoferece.

O horario novo so pode vir de `check_availability`: a tool recebe o `slot_token` e nada mais
(invariante 1, D-54). O servico e o do agendamento — remarcar nao troca de servico.

Sem `slot_token` a tool so decide a politica (`_without_slot`): e o caminho de
`can_reschedule: false`, onde o modelo nunca viu horario e portanto nao tem token. Fora do
prazo, `needs_human`; dentro, `slot_token_required`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.base import ToolContext, ToolResult, ToolSpec
from app.agent.tools.common import (
    FIND_APPOINTMENTS,
    RECHECK_AVAILABILITY,
    AppointmentView,
    appointment_facts,
    failure,
    load_appointment_view,
    needs_human,
    price_line,
    slot_signer,
)
from app.calendar.base import CalendarProvider, EventDraft, EventPatch
from app.calendar.sync_state import push_appointment_event
from app.core.config import Settings
from app.core.db import tenant_session
from app.core.telemetry import get_logger
from app.domain.models import Appointment
from app.scheduling import booking
from app.scheduling.availability import CalendarResolver
from app.scheduling.rules import ChangePolicy
from app.scheduling.slot_token import SlotTokenError

log = get_logger(__name__)

__all__ = ["RescheduleAppointmentArgs", "reschedule_appointment_tool"]

NAME = "reschedule_appointment"

_DESCRIPTION = (
    "Move um agendamento existente para outro horario. Fluxo: `find_appointments` para achar "
    "o agendamento (se houver mais de um, pergunte qual), `check_availability` para o MESMO "
    "servico (o `service_id` vem de find_appointments), e, depois que o cliente escolher, "
    "esta tool com o `appointment_id` e o `slot_token` do horario escolhido, copiado sem "
    "alterar. Nao existe outro jeito de remarcar: horario que nao veio de "
    "`check_availability` nao e aceito. Se o agendamento vier com `can_reschedule: false`, "
    "nao ofereca horarios: chame esta tool so com o `appointment_id` — ela encaminha a "
    "conversa para a equipe. Em caso de sucesso (`status: rescheduled`), confirme o novo "
    "horario com os dados devolvidos; o horario antigo foi liberado. Se o resultado for "
    "`slot_taken`, o agendamento atual continua valendo: chame `check_availability` de novo. "
    "Se for `needs_human`, avise que alguem da equipe vai continuar e nada mais. NUNCA diga "
    "que remarcou sem receber `rescheduled`."
)

_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "appointment_id": {
            "type": "string",
            "description": "O `appointment_id` do agendamento a mover, de find_appointments.",
        },
        "slot_token": {
            "type": "string",
            "description": "O `slot_token` do horario escolhido, copiado de check_availability. "
            "Omita so quando o agendamento tem `can_reschedule: false`.",
        },
    },
    "required": ["appointment_id"],
}


class RescheduleAppointmentArgs(BaseModel):
    appointment_id: uuid.UUID
    #: Opcional de proposito: quem nao pode remarcar por aqui (`can_reschedule: false`) nao
    #: tem token — nunca viu horario — e o modelo nao tem como fabricar um. Sem token, a
    #: tool so decide a politica; com a politica ok, pede o token.
    slot_token: str | None = Field(default=None, max_length=1000)


# ------------------------------------ respostas ------------------------------------

_BLOCKED: Final[Mapping[str, tuple[str, str]]] = {
    "too_late": (
        "remarcacao_fora_do_prazo",
        "Faltam menos horas para o atendimento do que a politica da casa exige para remarcar "
        "por aqui.",
    ),
    "not_allowed": (
        "remarcacao_nao_permitida",
        "A casa nao remarca por conversa: so a equipe faz isso.",
    ),
}

_FAILURES: Final[Mapping[str, tuple[str, str, str]]] = {
    "slot_taken": (
        "slot_taken",
        "Esse horario acabou de ser ocupado. O agendamento atual continua valendo.",
        RECHECK_AVAILABILITY,
    ),
    "same_slot": (
        "same_slot",
        "Esse ja e o horario do agendamento.",
        "ask the customer for a different time and call check_availability",
    ),
    "wrong_service": (
        "wrong_service",
        "Remarcar mantem o servico do agendamento, e esse horario e de outro servico.",
        "call check_availability for the service of the appointment",
    ),
    "invalid": (
        "invalid",
        "Esse horario nao pode ser usado.",
        RECHECK_AVAILABILITY,
    ),
    "not_found": (
        "appointment_not_found",
        "Nao existe esse agendamento para este cliente.",
        FIND_APPOINTMENTS,
    ),
    "not_active": (
        "appointment_not_active",
        "Esse agendamento nao esta mais ativo: ja foi cancelado ou remarcado.",
        FIND_APPOINTMENTS,
    ),
}


# ---------------------------------- fase 2: calendario ----------------------------------


@dataclass(frozen=True, slots=True)
class _Move:
    """O agendamento que saiu e o que entrou, ja lidos do banco."""

    old: AppointmentView
    new: AppointmentView


def _draft(ctx: ToolContext, view: AppointmentView) -> EventDraft:
    """O evento como `confirm_appointment` o escreve, com a marca de que foi remarcado."""
    who = view.subject_name or view.contact_name
    lines = []
    if view.contact_name or view.contact_phone:
        contact = " ".join(part for part in (view.contact_name, view.contact_phone) if part)
        lines.append(f"Contato: {contact}")
    if view.subject_name:
        lines.append(f"Atendido: {view.subject_name}")
    lines.append("Remarcado pelo assistente virtual.")
    appointment = view.appointment
    return EventDraft(
        summary=f"{view.service.name} - {who}" if who else view.service.name,
        start=appointment.starts_at,
        end=appointment.ends_at,
        timezone=view.label.timezone,
        description="\n".join(lines),
        tenant_ref=str(ctx.tenant_id),
    )


def _same_calendar(old: AppointmentView, new: AppointmentView) -> bool:
    return (
        old.provider.calendar_id == new.provider.calendar_id
        and old.provider.credentials_ref == new.provider.credentials_ref
    )


@dataclass(frozen=True, slots=True)
class _Handler:
    calendars: CalendarResolver
    settings: Settings | None

    async def __call__(self, ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        args = RescheduleAppointmentArgs.model_validate(arguments)
        policy = ChangePolicy.from_rules(ctx.config.scheduling)

        if args.slot_token is None:
            return await self._without_slot(ctx, args, policy)
        try:
            claims = slot_signer(self.settings).verify(
                args.slot_token,
                tenant_id=ctx.tenant_id,
                conversation_id=ctx.conversation_id,
                now=ctx.now,
            )
        except SlotTokenError as exc:
            log.warning("slot_token_recusado", motivo=exc.reason)
            return failure(
                "invalid_slot_token",
                "Esse horario nao veio de check_availability, ou a oferta venceu.",
                RECHECK_AVAILABILITY,
            )

        try:
            async with tenant_session(ctx.tenant_id, self.settings) as session:
                result = await booking.reschedule_appointment(
                    session,
                    tenant_id=ctx.tenant_id,
                    appointment_id=args.appointment_id,
                    contact_id=ctx.contact_id,
                    conversation_id=ctx.conversation_id,
                    provider_id=claims.provider_id,
                    service_id=claims.service_id,
                    resource_id=claims.resource_id,
                    starts_at=claims.starts_at,
                    policy=policy,
                    now=ctx.now,
                )
                move = await self._load_move(session, ctx, result)
        except Exception:
            # A transacao inteira voltou: nada foi remarcado. "Qualquer passo falha =
            # rollback + escalonamento" (12.2).
            log.exception("remarcacao_falhou", appointment_id=str(args.appointment_id))
            return needs_human(
                "falha_ao_remarcar",
                "Nao foi possivel remarcar agora e nada foi alterado: o agendamento original "
                "continua valendo.",
                original_kept=True,
            )

        if move is None:
            return self._refusal(result.status)

        # Repetir uma remarcacao ja espelhada nao toca o calendario: o evento ja esta la, e um
        # `create_event` agora duplicaria o que o `update_event` da primeira vez moveu.
        if move.new.appointment.sync_status != "synced" and not await self._mirror(ctx, move):
            return await self._roll_back(ctx, move)
        return self._success(ctx, move)

    async def _without_slot(
        self, ctx: ToolContext, args: RescheduleAppointmentArgs, policy: ChangePolicy
    ) -> ToolResult:
        """Chamada sem horario: so a politica fala.

        E o caminho de `can_reschedule: false`. Quem so a equipe pode remarcar nao viu
        horario nenhum — o modelo nao tem token, e nao deve ser mandado atras de um —, entao
        a tool decide aqui, antes de qualquer oferta. Com a politica ok, pede o horario.
        """
        async with tenant_session(ctx.tenant_id, self.settings) as session:
            verdict = await booking.check_change(
                session,
                tenant_id=ctx.tenant_id,
                appointment_id=args.appointment_id,
                contact_id=ctx.contact_id,
                kind="reschedule",
                policy=policy,
                now=ctx.now,
            )
        if verdict != "ok":
            return self._refusal(verdict)
        return failure(
            "slot_token_required",
            "Falta o horario novo: remarcar so aceita um horario de check_availability.",
            RECHECK_AVAILABILITY,
        )

    @staticmethod
    def _refusal(status: str) -> ToolResult:
        """Recusa por regra vira `needs_human`; o resto e erro que o modelo contorna."""
        if status in _BLOCKED:
            return needs_human(*_BLOCKED[status])
        return failure(*_FAILURES.get(status, _FAILURES["invalid"]))

    # ------------------------------------ banco ------------------------------------

    @staticmethod
    async def _load_move(
        session: AsyncSession, ctx: ToolContext, result: booking.RescheduleResult
    ) -> _Move | None:
        if result.status != "rescheduled":
            return None
        if result.appointment_id is None or result.previous_id is None:
            raise RuntimeError("remarcacao sem os dois agendamentos")
        old = await load_appointment_view(session, ctx, result.previous_id)
        new = await load_appointment_view(session, ctx, result.appointment_id)
        if old is None or new is None:
            # Levanta de dentro da transacao: ela volta inteira, em vez de a remarcacao
            # ficar gravada sem que a resposta consiga descreve-la.
            raise RuntimeError("agendamento remarcado nao pode ser relido")
        return _Move(old=old, new=new)

    async def _set_sync(
        self,
        ctx: ToolContext,
        move: _Move,
        *,
        status: str,
        event_id: str | None,
        clear_old: bool,
    ) -> None:
        """Grava o desfecho do espelho. Nunca levanta: a remarcacao ja esta no banco."""
        try:
            async with tenant_session(ctx.tenant_id, self.settings) as session:
                if clear_old:
                    # O evento e do agendamento novo agora; so um agendamento aponta para ele.
                    await session.execute(
                        update(Appointment)
                        .where(
                            Appointment.tenant_id == ctx.tenant_id,
                            Appointment.id == move.old.appointment.id,
                        )
                        .values(external_event_id=None)
                    )
                values: dict[str, Any] = {"sync_status": status}
                if event_id is not None:
                    values["external_event_id"] = event_id
                await session.execute(
                    update(Appointment)
                    .where(
                        Appointment.tenant_id == ctx.tenant_id,
                        Appointment.id == move.new.appointment.id,
                    )
                    .values(**values)
                )
        except Exception:
            log.exception(
                "remarcacao_sync_nao_gravado", appointment_id=str(move.new.appointment.id)
            )

    # --------------------------------- calendario ---------------------------------

    async def _mirror(self, ctx: ToolContext, move: _Move) -> bool:
        """Leva o evento para o horario novo. `False` = havia evento e ele nao andou.

        So `False` desfaz a remarcacao. Sem evento anterior, qualquer falha vira
        `pending_sync`/`sync_failed` e a remarcacao fica de pe, como na confirmacao.
        """
        old_event = move.old.appointment.external_event_id
        had_event = old_event is not None and move.old.provider.calendar_id is not None
        new_calendar = move.new.provider.calendar_id

        if not had_event and new_calendar is None:
            # Agenda propria dos dois lados: nao ha espelho a manter.
            await self._set_sync(ctx, move, status="synced", event_id=None, clear_old=False)
            return True

        if not had_event:
            return await self._create_only(ctx, move, new_calendar)
        assert old_event is not None and move.old.provider.calendar_id is not None
        try:
            return await self._move_event(ctx, move, old_event, new_calendar)
        except Exception:
            log.exception(
                "remarcacao_evento_nao_movido", appointment_id=str(move.new.appointment.id)
            )
            return False

    async def _create_only(self, ctx: ToolContext, move: _Move, calendar_id: str | None) -> bool:
        """Sem evento anterior: cria o do agendamento novo, e degrada como a confirmacao."""
        assert calendar_id is not None
        try:
            client = self.calendars(move.new.provider)
            if client is None:
                log.error("calendar_sem_credencial", appointment_id=str(move.new.appointment.id))
                await self._set_sync(
                    ctx, move, status="sync_failed", event_id=None, clear_old=False
                )
                return True
            async with tenant_session(ctx.tenant_id, self.settings) as session:
                await push_appointment_event(
                    session,
                    client,
                    appointment_id=move.new.appointment.id,
                    calendar_id=calendar_id,
                    draft=_draft(ctx, move.new),
                )
        except Exception:
            log.exception(
                "espelho_no_calendario_falhou", appointment_id=str(move.new.appointment.id)
            )
        return True

    async def _move_event(
        self, ctx: ToolContext, move: _Move, old_event: str, new_calendar: str | None
    ) -> bool:
        old_provider, new_provider = move.old.provider, move.new.provider
        assert old_provider.calendar_id is not None
        old_client = self.calendars(old_provider)
        if old_client is None:
            return False

        if new_calendar is None:
            # O destino tem agenda propria: o evento antigo so precisa sair do calendario.
            await old_client.delete_event(old_provider.calendar_id, old_event)
            await self._set_sync(ctx, move, status="synced", event_id=None, clear_old=True)
            return True

        if _same_calendar(move.old, move.new):
            appointment = move.new.appointment
            event = await old_client.update_event(
                new_calendar,
                old_event,
                EventPatch(
                    start=appointment.starts_at,
                    end=appointment.ends_at,
                    timezone=move.new.label.timezone,
                ),
            )
            await self._set_sync(ctx, move, status="synced", event_id=event.id, clear_old=True)
            return True

        new_client = self.calendars(new_provider)
        if new_client is None:
            return False
        created = await new_client.create_event(
            new_calendar,
            _draft(ctx, move.new),
            str(move.new.appointment.id),
        )
        try:
            await old_client.delete_event(old_provider.calendar_id, old_event)
        except Exception:
            await self._undo_create(new_client, new_calendar, created.id)
            raise
        await self._set_sync(ctx, move, status="synced", event_id=created.id, clear_old=True)
        return True

    @staticmethod
    async def _undo_create(client: CalendarProvider, calendar_id: str, event_id: str) -> None:
        """Desfaz o evento criado quando o antigo nao pode ser apagado. Melhor esforco."""
        try:
            await client.delete_event(calendar_id, event_id)
        except Exception:
            log.exception("remarcacao_evento_orfao", calendar_id=calendar_id, event_id=event_id)

    # --------------------------------- desfazer ---------------------------------

    async def _roll_back(self, ctx: ToolContext, move: _Move) -> ToolResult:
        try:
            async with tenant_session(ctx.tenant_id, self.settings) as session:
                restored = await booking.revert_reschedule(
                    session,
                    tenant_id=ctx.tenant_id,
                    appointment_id=move.new.appointment.id,
                    previous_id=move.old.appointment.id,
                    conversation_id=ctx.conversation_id,
                    now=ctx.now,
                )
        except Exception:
            log.exception("remarcacao_reversao_falhou", appointment_id=str(move.new.appointment.id))
            restored = False

        if restored:
            return needs_human(
                "falha_ao_remarcar",
                "Nao foi possivel atualizar a agenda do profissional, entao a remarcacao foi "
                "desfeita: o agendamento original continua valendo.",
                original_kept=True,
            )
        return needs_human(
            "falha_ao_remarcar",
            "Nao foi possivel concluir a remarcacao e o horario original tambem nao pode ser "
            "garantido. Nao diga qual horario vale: a equipe vai confirmar com o cliente.",
            original_kept=False,
        )

    # --------------------------------- resposta ---------------------------------

    @staticmethod
    def _success(ctx: ToolContext, move: _Move) -> ToolResult:
        view = move.new
        online = view.service.modality == "online"
        address = "" if online else ctx.config.template_values().get("address", "")
        content: dict[str, Any] = {
            "status": "rescheduled",
            **appointment_facts(view),
            "previous_appointment_id": str(move.old.appointment.id),
            "address": address,
            "price_line": price_line(view.service),
            "prep_line": view.service.prep_instructions or "",
            "message": "Remarcado. O horario antigo foi liberado. Confirme o novo horario.",
        }
        return ToolResult(content=content, entity="appointment", entity_id=view.appointment.id)


def reschedule_appointment_tool(
    calendars: CalendarResolver, *, settings: Settings | None = None
) -> ToolSpec:
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema=_INPUT_SCHEMA,
        handler=_Handler(calendars=calendars, settings=settings),
        kind="write",
        args_model=RescheduleAppointmentArgs,
    )
