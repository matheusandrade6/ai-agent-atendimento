"""Pecas compartilhadas pelas tools de agendamento (S15).

Nada aqui e uma tool. Sao as tres coisas que `check_availability`, `hold_slot`,
`confirm_appointment` e `find_appointments` repetiriam: como um horario e escrito para a
pessoa, de que timezone ele e, e o que o modelo deve fazer quando a tool diz "nao".

Instante ISO em resultado de tool e prova de horario
----------------------------------------------------
O guardrail de saida (`app.agent.temporal.evidence_from_tool_result`) trata **todo** instante
ISO com offset que aparece num resultado de tool como um horario real, que o modelo pode
oferecer. Por isso as tools de agendamento so escrevem instante ISO para o que a pessoa
pode ouvir como horario de atendimento — o inicio do slot ou do agendamento. Nunca o fim
(`ends_at`), nunca a validade do hold (`expires_at`) e nunca `created_at`: um `expires_at`
as 14:10 viraria prova de que existe vaga as 14:10. Duracao e validade saem como numero
de minutos, que a gramatica temporal nao le como horario.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.base import NEEDS_HUMAN, ToolContext, ToolResult
from app.core.config import Settings, get_settings
from app.core.time import to_tz
from app.domain.models import Appointment, Contact, Location, Provider, Service, Subject
from app.scheduling.confirmation_token import ConfirmationTokenSigner
from app.scheduling.slot_token import SlotTokenSigner

__all__ = [
    "FIND_APPOINTMENTS",
    "HAND_OFF_NOTICE",
    "RECHECK_AVAILABILITY",
    "AppointmentView",
    "ProviderLabel",
    "appointment_facts",
    "confirmation_signer",
    "failure",
    "human_date",
    "human_slot",
    "human_time",
    "load_appointment_view",
    "needs_human",
    "price_line",
    "provider_labels",
    "slot_signer",
]

#: Texto literal da 11.4: e o que o modelo le para saber o proximo passo apos `hold_expired`.
RECHECK_AVAILABILITY: Final = "call check_availability again"

#: Proximo passo quando o agendamento citado nao existe ou nao esta mais ativo.
FIND_APPOINTMENTS: Final = "call find_appointments and ask which appointment"

#: O que o modelo faz com um `needs_human`: avisar, sem prometer o que o humano decidira.
HAND_OFF_NOTICE: Final = (
    "tell the customer, in one short message, that someone from the team will take over; "
    "do not promise a new time, a refund or any outcome"
)

_WEEKDAYS: Final = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)
_MONTHS: Final = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)


# ----------------------------------- apresentacao -----------------------------------


def human_date(local: datetime) -> str:
    """`quarta-feira, 9 de setembro`. Recebe o instante ja na timezone de quem le."""
    return f"{_WEEKDAYS[local.weekday()]}, {local.day} de {_MONTHS[local.month - 1]}"


def human_time(local: datetime) -> str:
    """`14h` ou `14h30`: do jeito que se fala, e que a gramatica temporal le de volta."""
    return f"{local.hour}h" if local.minute == 0 else f"{local.hour}h{local.minute:02d}"


def human_slot(local: datetime) -> str:
    """`quarta-feira, 9 de setembro, às 14h` — o campo `human` da 11.4."""
    return f"{human_date(local)}, às {human_time(local)}"


def price_line(service: Service) -> str:
    if service.price_cents is not None:
        reais = service.price_cents / 100
        return f"R$ {reais:.2f}".replace(".", ",")
    return service.price_note or ""


# ----------------------------------- profissional -----------------------------------


@dataclass(frozen=True, slots=True)
class ProviderLabel:
    name: str
    #: Timezone de quem le o horario: a da unidade do profissional, ou a do tenant.
    timezone: str

    def local(self, moment: datetime) -> datetime:
        return to_tz(moment, self.timezone)


async def provider_labels(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    provider_ids: Collection[uuid.UUID],
    *,
    default_timezone: str,
) -> dict[uuid.UUID, ProviderLabel]:
    """Nome e timezone de cada profissional — a mesma regra de `find_availability`.

    A timezone e a da unidade do profissional (`locations.timezone`); sem unidade, a do
    tenant. Se isto divergir da regra do motor, o horario que a pessoa le sai deslocado
    em relacao ao que o motor calculou.
    """
    if not provider_ids:
        return {}
    rows = (
        await session.execute(
            select(Provider.id, Provider.name, Provider.display_name, Location.timezone)
            .outerjoin(
                Location,
                and_(Location.id == Provider.location_id, Location.tenant_id == tenant_id),
            )
            .where(Provider.tenant_id == tenant_id, Provider.id.in_(provider_ids))
        )
    ).all()
    return {
        row.id: ProviderLabel(
            name=row.display_name or row.name, timezone=row.timezone or default_timezone
        )
        for row in rows
    }


# ------------------------------------ resultado ------------------------------------


def failure(status: str, message: str, suggested_action: str) -> ToolResult:
    """Falha recuperavel: o modelo le o que houve e o que fazer, e o motor a conta como erro.

    `is_error=True` nao e enfeite: `next_stage` so promove o estagio (`confirming`,
    `booked`) para tool que terminou bem, e o guardrail de saida so le horario de resultado
    que nao e erro.
    """
    return ToolResult(
        content={"status": status, "message": message, "suggested_action": suggested_action},
        is_error=True,
    )


def needs_human(reason: str, message: str, **facts: object) -> ToolResult:
    """Pedido que o codigo nao pode atender: o motor abre o handoff no mesmo turno (D-60).

    E erro (`is_error=True`) pelo mesmo motivo de `failure`: nada foi remarcado nem cancelado,
    entao o funil nao anda e o guardrail nao toma o resultado como prova de horario. `reason`
    e o rotulo fechado que o handoff grava (`policy:<reason>`); `message` e para o modelo.
    Numero de horas fica de fora de proposito — "4 horas" no texto do modelo seria lido pelo
    guardrail de saida como um horario.
    """
    return ToolResult(
        content={
            "status": NEEDS_HUMAN,
            "reason": reason,
            "message": message,
            "suggested_action": HAND_OFF_NOTICE,
            **facts,
        },
        is_error=True,
    )


# ----------------------------------- agendamento ------------------------------------


@dataclass(frozen=True, slots=True)
class AppointmentView:
    """Um agendamento do contato da conversa, com o que as tools de S16 precisam dele."""

    appointment: Appointment
    service: Service
    provider: Provider
    subject_name: str
    label: ProviderLabel
    contact_name: str | None = None
    contact_phone: str | None = None

    @property
    def starts_local(self) -> datetime:
        return self.label.local(self.appointment.starts_at)


async def load_appointment_view(
    session: AsyncSession, ctx: ToolContext, appointment_id: uuid.UUID
) -> AppointmentView | None:
    """Carrega o agendamento **do contato da conversa**; de outro contato, devolve `None`."""
    row = (
        await session.execute(
            select(Appointment, Service, Provider, Contact)
            .join(Service, Service.id == Appointment.service_id)
            .join(Provider, Provider.id == Appointment.provider_id)
            .join(Contact, Contact.id == Appointment.contact_id)
            .where(
                Appointment.tenant_id == ctx.tenant_id,
                Appointment.id == appointment_id,
                Appointment.contact_id == ctx.contact_id,
            )
        )
    ).first()
    if row is None:
        return None
    appointment, service, provider, contact = row
    subject = ""
    if appointment.subject_id is not None:
        subject = (
            await session.scalar(
                select(Subject.name).where(
                    Subject.tenant_id == ctx.tenant_id, Subject.id == appointment.subject_id
                )
            )
            or ""
        )
    labels = await provider_labels(
        session,
        ctx.tenant_id,
        {provider.id},
        default_timezone=ctx.config.business_hours.timezone,
    )
    label = labels.get(provider.id) or ProviderLabel(
        name=provider.display_name or provider.name, timezone=ctx.config.business_hours.timezone
    )
    return AppointmentView(
        appointment=appointment,
        service=service,
        provider=provider,
        subject_name=subject or appointment.intake.get("subject_name") or "",
        label=label,
        contact_name=contact.name,
        contact_phone=contact.phone_e164,
    )


def appointment_facts(view: AppointmentView) -> dict[str, str]:
    """Os fatos de um agendamento que o modelo pode repetir ao cliente.

    So o inicio do atendimento sai como instante (D-55): e o que a pessoa ouve como horario.
    """
    local = view.starts_local
    return {
        "appointment_id": str(view.appointment.id),
        "service": view.service.name,
        "subject_name": view.subject_name,
        "starts_at": local.isoformat(),
        "human": human_slot(local),
        "provider_name": view.label.name,
    }


# --------------------------------------- chave ---------------------------------------


def slot_signer(settings: Settings | None) -> SlotTokenSigner:
    settings = settings or get_settings()
    return SlotTokenSigner(
        settings.slot_token_secret, ttl=timedelta(minutes=settings.slot_token_ttl_minutes)
    )


def confirmation_signer(settings: Settings | None) -> ConfirmationTokenSigner:
    settings = settings or get_settings()
    return ConfirmationTokenSigner(
        settings.slot_token_secret, ttl=timedelta(minutes=settings.confirmation_token_ttl_minutes)
    )
