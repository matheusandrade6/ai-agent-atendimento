"""Estado de sincronizacao do agendamento com o calendario (14.3, "Resiliencia").

O agendamento **ja existe** no banco quando o evento vai para o Google — o banco e a
fonte de verdade, o calendario e o espelho. Por isso falha de calendario nao desfaz
agendamento: ela muda `appointments.sync_status`.

- `synced` — evento criado (ou ja existia: a chamada e idempotente).
- `pending_sync` — o Google ficou indisponivel alem das retentativas. Um job de
  reconciliacao pode tentar de novo; o agente avisa que confirmara em instantes.
- `sync_failed` — falha que retentar nao resolve (credencial revogada, payload recusado).
  Precisa de humano.

Chame **fora** da transacao que segura o `pg_advisory_xact_lock`: a retentativa pode levar
dezenas de segundos, e segurar o lock do profissional/dia durante esse tempo bloquearia
os outros agendamentos dele por causa de uma API de terceiros.
"""

from __future__ import annotations

import uuid
from typing import Literal

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar.base import (
    CalendarError,
    CalendarProvider,
    CalendarSyncPending,
    EventDraft,
    ExternalEvent,
)
from app.domain.models import Appointment

log = structlog.get_logger(__name__)

SyncStatus = Literal["synced", "pending_sync", "sync_failed"]


async def push_appointment_event(
    session: AsyncSession,
    provider: CalendarProvider,
    *,
    appointment_id: uuid.UUID,
    calendar_id: str,
    draft: EventDraft,
) -> tuple[SyncStatus, ExternalEvent | None]:
    """Cria o evento do agendamento e grava o desfecho em `appointments`.

    Nunca propaga falha de calendario: devolve o `sync_status` resultante. A `session`
    deve ser a do tenant (RLS) e o commit e de quem a abriu.
    """
    status: SyncStatus
    event: ExternalEvent | None = None
    try:
        event = await provider.create_event(calendar_id, draft, str(appointment_id))
        status = "synced"
    except CalendarSyncPending as exc:
        status = "pending_sync"
        # Este log e o alerta ao operador da 14.3: o nivel `error` e o que a observabilidade
        # (secao 17) liga a um aviso.
        log.error(
            "calendar_sync_pending",
            appointment_id=str(appointment_id),
            attempts=exc.attempts,
            reason=str(exc),
        )
    except CalendarError as exc:  # auth, permanente ou qualquer outra que nao passa
        status = "sync_failed"
        log.error(
            "calendar_sync_failed",
            appointment_id=str(appointment_id),
            error=type(exc).__name__,
            reason=str(exc),
        )

    values: dict[str, str] = {"sync_status": status}
    if event is not None:
        values["external_event_id"] = event.id
    await session.execute(
        update(Appointment).where(Appointment.id == appointment_id).values(**values)
    )
    return status, event
