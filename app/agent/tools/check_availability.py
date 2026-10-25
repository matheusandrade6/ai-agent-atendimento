"""Tool de leitura: horarios realmente disponiveis (secao 11.4).

E a **unica fonte de horario** que o agente tem (invariante 1). Embrulha `find_availability`
(S13) e assina um `slot_token` por horario; sem o token nao ha como reservar (`hold_slot`).

Onde o contrato desta tool se afasta da 11.4
--------------------------------------------
- **Sem `ends_at`.** A 11.4 lista `ends_at` no slot. O guardrail de saida le todo instante
  ISO de um resultado de tool como horario oferecivel; o fim de um atendimento as 19:00
  provaria uma vaga as 19:00 que nao existe. A duracao sai em `duration_minutes`
  (`app.agent.tools.common`, D-55).
- **`offset`.** "Mais horarios" (RF-17) e `offset` sobre a mesma ordenacao do motor, ja
  suportado por `AvailabilityRequest`; sem ele o modelo so conseguiria repetir a mesma lista.
- **`limit` nao passa do tenant.** `limit` omitido vale `scheduling.slots_per_offer`, e um
  `limit` maior e cortado nele (RF-17: "no maximo N slots por vez").
- **`next_available_date`** e a data do primeiro horario devolvido; quando nao ha nenhum na
  janela, e a do primeiro horario depois dela, ate o horizonte do tenant (`null` se nao ha).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final, Self

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.base import ToolContext, ToolResult, ToolSpec
from app.agent.tools.common import (
    failure,
    human_slot,
    provider_labels,
    slot_signer,
)
from app.core.config import Settings
from app.core.db import tenant_session
from app.core.telemetry import get_logger
from app.domain.tenant_config import Weekday
from app.scheduling.availability import (
    AvailabilityRequest,
    AvailabilityResult,
    CalendarResolver,
    UnknownService,
    find_availability,
)
from app.scheduling.rules import Period, Preferences, SchedulingPolicy
from app.scheduling.slot_token import SlotClaims

log = get_logger(__name__)

__all__ = ["CheckAvailabilityArgs", "check_availability_tool"]

NAME = "check_availability"

#: Janela maxima de uma consulta. O `freebusy` e o banco sao lidos para a janela inteira.
MAX_WINDOW_DAYS: Final = 62

_DESCRIPTION = (
    "Retorna os horarios realmente disponiveis para um servico. Use SEMPRE antes de "
    "mencionar qualquer horario ao cliente e nunca invente horarios: so existem os que "
    "esta ferramenta devolve. Cada horario traz um `slot_token` — e com ele que voce "
    "reserva (`hold_slot`); nao ha outro jeito de reservar. Para 'mais horarios', chame de "
    "novo somando ao `offset` quantos horarios ja mostrou. `next_available_date` diz o "
    "primeiro dia com vaga quando a janela pedida esta vazia. Se `incomplete` vier "
    "verdadeiro, alguma agenda nao respondeu: nao diga que nao ha vaga, diga que nao "
    "conseguiu consultar tudo."
)

_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "service_id": {"type": "string", "description": "ID do catalogo (list_services)."},
        "provider_id": {
            "type": "string",
            "description": "Opcional. Omitir = qualquer profissional apto.",
        },
        "date_from": {"type": "string", "format": "date", "description": "AAAA-MM-DD, inclusivo."},
        "date_to": {"type": "string", "format": "date", "description": "AAAA-MM-DD, inclusivo."},
        "preferred_weekdays": {
            "type": "array",
            "items": {"type": "string", "enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
            "description": "Ordena a oferta; nao filtra.",
        },
        "preferred_periods": {
            "type": "array",
            "items": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
            "description": "Ordena a oferta; nao filtra.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "description": "Omitido = o padrao do estabelecimento, que tambem e o teto.",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Quantos horarios pular, para 'mais horarios'.",
        },
    },
    "required": ["service_id", "date_from", "date_to"],
}


class CheckAvailabilityArgs(BaseModel):
    service_id: uuid.UUID
    date_from: date
    date_to: date
    provider_id: uuid.UUID | None = None
    preferred_weekdays: list[Weekday] = Field(default_factory=list)
    preferred_periods: list[Period] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=10)
    offset: int = Field(default=0, ge=0, le=200)

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.date_to < self.date_from:
            raise ValueError("date_to nao pode ser anterior a date_from")
        if (self.date_to - self.date_from).days > MAX_WINDOW_DAYS:
            raise ValueError(f"janela maior que {MAX_WINDOW_DAYS} dias")
        return self


_INCOMPLETE_NOTE: Final = (
    "Alguma agenda nao respondeu agora, entao esta lista pode estar incompleta. Nao diga ao "
    "cliente que nao ha vaga; diga que nao conseguiu consultar tudo e, se nenhum horario "
    "servir, chame escalate_to_human."
)


@dataclass(frozen=True, slots=True)
class _Handler:
    calendars: CalendarResolver
    settings: Settings | None

    async def __call__(self, ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        args = CheckAvailabilityArgs.model_validate(arguments)
        signer = slot_signer(self.settings)
        scheduling = ctx.config.scheduling
        per_offer = scheduling.slots_per_offer
        wanted = min(args.limit, per_offer) if args.limit else per_offer
        preferences = Preferences(
            periods=frozenset(args.preferred_periods), weekdays=frozenset(args.preferred_weekdays)
        )
        tenant_tz = ctx.config.business_hours.timezone

        async with tenant_session(ctx.tenant_id, self.settings) as session:
            try:
                # Um a mais que o pedido: e assim que se sabe se ha `has_more` sem outra consulta.
                found = await find_availability(
                    session,
                    tenant_id=ctx.tenant_id,
                    config=ctx.config,
                    request=AvailabilityRequest(
                        service_id=args.service_id,
                        date_from=args.date_from,
                        date_to=args.date_to,
                        now=ctx.now,
                        provider_id=args.provider_id,
                        preferences=preferences,
                        limit=wanted + 1,
                        offset=args.offset,
                    ),
                    calendars=self.calendars,
                )
            except UnknownService:
                return failure(
                    "unknown_service",
                    "Nao existe servico ativo com esse service_id.",
                    "call list_services and use an id from its result",
                )

            visible = found.slots[:wanted]
            labels = await provider_labels(
                session,
                ctx.tenant_id,
                {slot.provider_id for slot in visible},
                default_timezone=tenant_tz,
            )
            next_date: date | None = visible[0].local_start.date() if visible else None
            if not visible:
                next_date = await self._next_date_after(session, ctx, args, preferences)

        slots: list[dict[str, Any]] = []
        for slot in visible:
            label = labels.get(slot.provider_id)
            slots.append(
                {
                    "slot_token": signer.sign(
                        SlotClaims(
                            provider_id=slot.provider_id,
                            service_id=args.service_id,
                            starts_at=slot.start,
                            resource_id=slot.resource_id,
                        ),
                        tenant_id=ctx.tenant_id,
                        conversation_id=ctx.conversation_id,
                        now=ctx.now,
                    ),
                    "starts_at": slot.local_start.isoformat(),
                    "provider_id": str(slot.provider_id),
                    "provider_name": label.name if label else "",
                    "duration_minutes": int((slot.end - slot.start).total_seconds() // 60),
                    "human": human_slot(slot.local_start),
                }
            )

        content: dict[str, Any] = {
            "slots": slots,
            "has_more": len(found.slots) > wanted,
            "next_available_date": next_date.isoformat() if next_date else None,
        }
        if found.unavailable:
            # O motivo fica no log: pode citar credencial e calendario, e nao e para o prompt.
            log.warning(
                "agenda_nao_consultada",
                donos=[str(owner) for owner in found.unavailable],
                motivos=sorted(set(found.unavailable.values())),
            )
            content["incomplete"] = True
            content["note"] = _INCOMPLETE_NOTE
        return ToolResult(content=content)

    async def _next_date_after(
        self,
        session: AsyncSession,
        ctx: ToolContext,
        args: CheckAvailabilityArgs,
        preferences: Preferences,
    ) -> date | None:
        """Primeiro dia com vaga depois da janela pedida, ate o horizonte do tenant."""
        policy = SchedulingPolicy.from_rules(ctx.config.scheduling)
        horizon = policy.last_day(ctx.now, ctx.config.business_hours.timezone)
        first = args.date_to + timedelta(days=1)
        if first > horizon:
            return None
        later: AvailabilityResult = await find_availability(
            session,
            tenant_id=ctx.tenant_id,
            config=ctx.config,
            request=AvailabilityRequest(
                service_id=args.service_id,
                date_from=first,
                date_to=min(horizon, first + timedelta(days=MAX_WINDOW_DAYS)),
                now=ctx.now,
                provider_id=args.provider_id,
                preferences=preferences,
                limit=1,
            ),
            calendars=self.calendars,
        )
        return later.slots[0].local_start.date() if later.slots else None


def check_availability_tool(
    calendars: CalendarResolver, *, settings: Settings | None = None
) -> ToolSpec:
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema=_INPUT_SCHEMA,
        handler=_Handler(calendars=calendars, settings=settings),
        kind="read",
        args_model=CheckAvailabilityArgs,
    )
