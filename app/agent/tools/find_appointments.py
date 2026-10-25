"""Tool de leitura: agendamentos futuros do contato (secao 11.4, 12.2, 12.3).

E o ponto de partida de remarcar e cancelar (S16): o `appointment_id` que elas recebem
sai daqui. O filtro e **sempre** o contato da conversa — `contact_id` vem do contexto, nao
de argumento, entao o modelo nao tem como listar os agendamentos de outra pessoa
(invariante 3, no espirito: o que identifica quem pergunta nunca e escolhido pelo modelo).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel
from sqlalchemy import select

from app.agent.tools.base import ToolContext, ToolResult, ToolSpec
from app.agent.tools.common import human_slot, provider_labels
from app.core.config import Settings
from app.core.db import tenant_session
from app.domain.models import Appointment, Service, Subject
from app.scheduling.availability import ACTIVE_APPOINTMENT_STATUSES

__all__ = ["FindAppointmentsArgs", "find_appointments_tool"]

NAME = "find_appointments"

#: Teto da lista. Quem tem mais que isto por vir precisa de um humano, nao de uma lista.
_LIMIT: Final = 10

_DESCRIPTION = (
    "Lista os agendamentos futuros do cliente desta conversa, do mais proximo ao mais "
    "distante. Use para remarcar ou cancelar (se houver mais de um, pergunte qual) e para "
    "responder 'quando e minha consulta?'. Cada item traz `appointment_id`. So os "
    "agendamentos devolvidos existem: nao cite outro."
)

_INPUT_SCHEMA: Mapping[str, Any] = {"type": "object", "properties": {}, "required": []}


class FindAppointmentsArgs(BaseModel):
    """Sem argumentos: o filtro (o contato da conversa) vem do contexto."""


@dataclass(frozen=True, slots=True)
class _Handler:
    settings: Settings | None

    async def __call__(self, ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        async with tenant_session(ctx.tenant_id, self.settings) as session:
            rows = (
                await session.execute(
                    select(Appointment, Service.name, Subject.name)
                    .join(Service, Service.id == Appointment.service_id)
                    .outerjoin(Subject, Subject.id == Appointment.subject_id)
                    .where(
                        Appointment.tenant_id == ctx.tenant_id,
                        Appointment.contact_id == ctx.contact_id,
                        Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
                        Appointment.starts_at >= ctx.now,
                    )
                    .order_by(Appointment.starts_at, Appointment.id)
                    .limit(_LIMIT)
                )
            ).all()
            labels = await provider_labels(
                session,
                ctx.tenant_id,
                {appointment.provider_id for appointment, _, _ in rows},
                default_timezone=ctx.config.business_hours.timezone,
            )

        appointments: list[dict[str, Any]] = []
        for appointment, service_name, subject_name in rows:
            label = labels.get(appointment.provider_id)
            local = (
                label.local(appointment.starts_at)
                if label
                else appointment.starts_at.astimezone(ctx.now.tzinfo)
            )
            appointments.append(
                {
                    "appointment_id": str(appointment.id),
                    "service": service_name,
                    "subject_name": subject_name or appointment.intake.get("subject_name") or "",
                    "starts_at": local.isoformat(),
                    "human": human_slot(local),
                    "provider_name": label.name if label else "",
                    "status": appointment.status,
                }
            )
        return ToolResult(content={"appointments": appointments, "count": len(appointments)})


def find_appointments_tool(*, settings: Settings | None = None) -> ToolSpec:
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema=_INPUT_SCHEMA,
        handler=_Handler(settings=settings),
        kind="read",
        args_model=FindAppointmentsArgs,
    )
