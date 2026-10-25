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

from app.agent.tools.base import ToolResult
from app.core.config import Settings, get_settings
from app.core.time import to_tz
from app.domain.models import Location, Provider, Service
from app.scheduling.slot_token import SlotTokenSigner

__all__ = [
    "RECHECK_AVAILABILITY",
    "ProviderLabel",
    "failure",
    "human_date",
    "human_slot",
    "human_time",
    "price_line",
    "provider_labels",
    "slot_signer",
]

#: Texto literal da 11.4: e o que o modelo le para saber o proximo passo apos `hold_expired`.
RECHECK_AVAILABILITY: Final = "call check_availability again"

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


# --------------------------------------- chave ---------------------------------------


def slot_signer(settings: Settings | None) -> SlotTokenSigner:
    settings = settings or get_settings()
    return SlotTokenSigner(
        settings.slot_token_secret, ttl=timedelta(minutes=settings.slot_token_ttl_minutes)
    )
