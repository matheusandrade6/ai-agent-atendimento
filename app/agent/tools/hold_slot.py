"""Tool de escrita: reserva temporaria de um horario oferecido (secao 11.4, RF-18).

Por que o unico argumento e o token
-----------------------------------
Esta tool **nao** aceita `provider_id`, `service_id` nem `starts_at`. Quem decide o horario e
o `slot_token` que `check_availability` assinou: dele saem profissional, recurso, servico e
inicio. O modelo escolhe entre os horarios oferecidos; nao tem como pedir um que ninguem
ofereceu, porque nao ha campo onde escreve-lo e nao ha como forjar o token (invariante 1,
`app.scheduling.slot_token`). Token adulterado, vencido ou de outra conversa volta como
`invalid_slot_token`, sem tocar o banco.

Idempotencia entre turnos
-------------------------
Reservar duas vezes o mesmo horario na mesma conversa devolve a **mesma** reserva em vez de
`slot_taken`: sem isso, o cliente que responde "pode ser esse" depois do modelo ja ter
reservado receberia "horario ocupado" por causa do hold dele mesmo. A chave natural e
(conversa, profissional, servico, inicio). Reservar outro horario libera as reservas
anteriores da conversa — nao ha razao para segurar tres vagas para uma pessoa so — mas so se
a nova der certo (`_hold_replacing_previous`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.base import ToolContext, ToolResult, ToolSpec
from app.agent.tools.common import (
    RECHECK_AVAILABILITY,
    failure,
    human_slot,
    provider_labels,
    slot_signer,
)
from app.core.config import Settings
from app.core.db import tenant_session
from app.core.telemetry import get_logger
from app.domain.models import Hold
from app.scheduling.booking import HoldResult
from app.scheduling.booking import hold_slot as book_slot
from app.scheduling.slot_token import SlotClaims, SlotTokenError

log = get_logger(__name__)

__all__ = ["HoldSlotArgs", "hold_slot_tool"]

NAME = "hold_slot"

_DESCRIPTION = (
    "Reserva por alguns minutos um dos horarios que `check_availability` devolveu, depois "
    "que o cliente escolheu um deles. Passe o `slot_token` do horario escolhido, exatamente "
    "como veio, sem alterar nada. Nao existe outro jeito de reservar: um horario que nao "
    "veio de `check_availability` nao pode ser reservado. Devolve `hold_id`, que "
    "`confirm_appointment` usa para efetivar. Se o resultado for `slot_taken` ou "
    "`invalid_slot_token`, chame `check_availability` de novo e ofereca outros horarios."
)

_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "slot_token": {
            "type": "string",
            "description": "O `slot_token` do horario escolhido, copiado de check_availability.",
        }
    },
    "required": ["slot_token"],
}


class HoldSlotArgs(BaseModel):
    slot_token: str = Field(min_length=1, max_length=1000)


async def _own_active_hold(
    session: AsyncSession, ctx: ToolContext, claims: SlotClaims
) -> HoldResult | None:
    """A reserva vigente que esta conversa ja tem para este exato horario, se houver."""
    resource = (
        Hold.resource_id.is_(None)
        if claims.resource_id is None
        else Hold.resource_id == claims.resource_id
    )
    row = (
        await session.execute(
            select(Hold.id, Hold.starts_at, Hold.ends_at, Hold.expires_at).where(
                Hold.tenant_id == ctx.tenant_id,
                Hold.conversation_id == ctx.conversation_id,
                Hold.provider_id == claims.provider_id,
                Hold.service_id == claims.service_id,
                Hold.starts_at == claims.starts_at,
                resource,
                Hold.status == "active",
                Hold.expires_at > ctx.now,
            )
        )
    ).first()
    if row is None:
        return None
    return HoldResult(
        status="held",
        hold_id=row.id,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        expires_at=row.expires_at,
    )


async def _hold_replacing_previous(
    session: AsyncSession, ctx: ToolContext, claims: SlotClaims, ttl: timedelta
) -> HoldResult:
    """Reserva o horario novo no lugar das reservas anteriores da conversa.

    A liberacao vem **antes** da tentativa, e nao depois: dois horarios vizinhos (08:00 e
    08:15) se sobrepoem, e a reserva antiga da propria conversa bloquearia a nova. Mas se a
    nova falha (`slot_taken`), a antiga volta — quem pediu para trocar nao perde o que ja
    tinha por causa de uma troca que nao aconteceu. Por isso a liberacao mora num savepoint.
    """
    savepoint = await session.begin_nested()
    try:
        await session.execute(
            update(Hold)
            .where(
                Hold.tenant_id == ctx.tenant_id,
                Hold.conversation_id == ctx.conversation_id,
                Hold.status == "active",
            )
            .values(status="expired")
            .execution_options(synchronize_session=False)
        )
        held = await book_slot(
            session,
            tenant_id=ctx.tenant_id,
            conversation_id=ctx.conversation_id,
            provider_id=claims.provider_id,
            service_id=claims.service_id,
            starts_at=claims.starts_at,
            now=ctx.now,
            ttl=ttl,
            resource_id=claims.resource_id,
        )
    except BaseException:
        await savepoint.rollback()
        raise
    if held.status == "held":
        await savepoint.commit()
    else:
        await savepoint.rollback()
    return held


def _minutes_left(expires_at: datetime, now: datetime) -> int:
    return max(1, math.ceil((expires_at - now) / timedelta(minutes=1)))


@dataclass(frozen=True, slots=True)
class _Handler:
    settings: Settings | None

    async def __call__(self, ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        args = HoldSlotArgs.model_validate(arguments)
        try:
            claims = slot_signer(self.settings).verify(
                args.slot_token,
                tenant_id=ctx.tenant_id,
                conversation_id=ctx.conversation_id,
                now=ctx.now,
            )
        except SlotTokenError as exc:
            # Sinal de que algo tentou um horario que o motor nao ofereceu — ou de que a
            # oferta venceu. O motivo vai para o log; o modelo so recebe o proximo passo.
            log.warning("slot_token_recusado", motivo=exc.reason)
            return failure(
                "invalid_slot_token",
                "Esse horario nao veio de check_availability, ou a oferta venceu.",
                RECHECK_AVAILABILITY,
            )

        ttl = timedelta(minutes=ctx.config.scheduling.hold_ttl_minutes)
        async with tenant_session(ctx.tenant_id, self.settings) as session:
            held = await _own_active_hold(session, ctx, claims)
            if held is None:
                held = await _hold_replacing_previous(session, ctx, claims, ttl)
            if held.status != "held" or held.hold_id is None or held.expires_at is None:
                if held.status == "invalid":
                    log.warning("hold_recusado_pelo_banco", motivo=held.reason)
                return self._refusal(held)

            labels = await provider_labels(
                session,
                ctx.tenant_id,
                {claims.provider_id},
                default_timezone=ctx.config.business_hours.timezone,
            )

        label = labels.get(claims.provider_id)
        local = (
            label.local(claims.starts_at) if label else claims.starts_at.astimezone(ctx.now.tzinfo)
        )
        return ToolResult(
            content={
                "status": "held",
                "hold_id": str(held.hold_id),
                "starts_at": local.isoformat(),
                "human": human_slot(local),
                "provider_name": label.name if label else "",
                # Minutos, nao instante: um `expires_at` ISO viraria "horario oferecivel"
                # para o guardrail de saida (`app.agent.tools.common`).
                "expires_in_minutes": _minutes_left(held.expires_at, ctx.now),
            },
            entity="hold",
            entity_id=held.hold_id,
        )

    @staticmethod
    def _refusal(held: HoldResult) -> ToolResult:
        if held.status == "slot_taken":
            return failure(
                "slot_taken", "Esse horario acabou de ser ocupado.", RECHECK_AVAILABILITY
            )
        return failure("invalid", "Esse horario nao pode ser reservado.", RECHECK_AVAILABILITY)


def hold_slot_tool(*, settings: Settings | None = None) -> ToolSpec:
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema=_INPUT_SCHEMA,
        handler=_Handler(settings=settings),
        kind="write",
        args_model=HoldSlotArgs,
    )
