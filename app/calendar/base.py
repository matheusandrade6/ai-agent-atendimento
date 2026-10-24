"""Contrato de calendario (secao 14.3) e os tipos que atravessam a fronteira.

Nenhum tipo aqui sabe que o Google existe (14.4): `ExternalEvent` e `Interval` sao o
vocabulario que o motor de disponibilidade (S13) e o sync reverso (S20) falam, e e o que
um futuro Outlook ou calendario proprio teria de devolver.

As excecoes existem para quem **chama** decidir o que fazer com a falha, nao para
descrever o erro — o mesmo desenho de `app.channels.base`:

- `CalendarSyncPending` — esgotou as retentativas por falha que *provavelmente passa*
  (5xx, limite de taxa, rede). O agendamento fica `pending_sync`, o operador e avisado.
- `CalendarAuthError` — a credencial morreu (`invalid_grant`, acesso revogado). Retentar
  nao adianta: so o cliente refazendo o consentimento resolve.
- `CalendarPermanentError` — 4xx que nao passa com retentativa (payload recusado,
  calendario inexistente). O agendamento fica `sync_failed`.
- `CalendarQueryError` — `freebusy` nao respondeu por algum calendario. Carrega o que
  veio, mas quem consome **nao pode** tratar o calendario ausente como livre (invariante
  1: o sistema nunca inventa disponibilidade).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

EventStatus = Literal["confirmed", "tentative", "cancelled"]


@dataclass(frozen=True, slots=True)
class Interval:
    """Intervalo ocupado `[start, end)`, sempre com timezone (UTC na saida do provider)."""

    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class EventDraft:
    """O que o produto quer ver no calendario. `timezone` e a IANA da unidade."""

    summary: str
    start: datetime
    end: datetime
    timezone: str
    description: str | None = None
    attendee_emails: Sequence[str] = ()
    #: Vai para `extendedProperties.private.datamind_tenant` (14.3).
    tenant_ref: str | None = None


@dataclass(frozen=True, slots=True)
class EventPatch:
    """Campos a alterar; `None` significa "nao mexe". `timezone` acompanha start/end."""

    summary: str | None = None
    description: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    timezone: str | None = None
    status: EventStatus | None = None


@dataclass(frozen=True, slots=True)
class ExternalEvent:
    """Evento como o calendario o enxerga.

    `start`/`end` sao `None` em evento removido devolvido por sync incremental: o
    Google manda so o id e o `status=cancelled`. `appointment_id` vem de
    `extendedProperties.private` e e o que separa "evento nosso" de ocupacao externa.
    """

    id: str
    status: EventStatus
    start: datetime | None = None
    end: datetime | None = None
    ical_uid: str | None = None
    summary: str | None = None
    appointment_id: str | None = None
    all_day: bool = False


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Resultado de `sync_changes`.

    `full_sync=True` avisa que o token foi recusado (`410 Gone`) e o provider releu a
    agenda do zero: o consumidor nao pode assumir que o que **nao** veio continua igual.
    `next_sync_token` e o que deve ser persistido em `providers.sync_token`.
    """

    events: Sequence[ExternalEvent]
    next_sync_token: str
    full_sync: bool = False


class CalendarError(Exception):
    """Base das falhas de calendario."""


class CalendarSyncPending(CalendarError):
    def __init__(self, message: str, *, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


class CalendarAuthError(CalendarError):
    """Credencial invalida ou revogada."""


class CalendarPermanentError(CalendarError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class CalendarNotFound(CalendarPermanentError):
    """Calendario ou evento nao existe."""


class CalendarQueryError(CalendarError):
    def __init__(
        self,
        failed: Mapping[str, str],
        partial: Mapping[str, list[Interval]],
    ) -> None:
        super().__init__(
            "freebusy sem resposta para: "
            + ", ".join(f"{cal} ({reason})" for cal, reason in sorted(failed.items()))
        )
        self.failed: dict[str, str] = dict(failed)
        self.partial: dict[str, list[Interval]] = {k: list(v) for k, v in partial.items()}


class CalendarProvider(Protocol):
    async def get_busy(
        self, calendar_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, list[Interval]]: ...

    async def create_event(
        self, calendar_id: str, event: EventDraft, idempotency_key: str
    ) -> ExternalEvent: ...

    async def update_event(
        self, calendar_id: str, event_id: str, patch: EventPatch
    ) -> ExternalEvent: ...

    async def delete_event(self, calendar_id: str, event_id: str) -> None: ...

    async def sync_changes(self, calendar_id: str, sync_token: str | None) -> SyncResult: ...

    async def aclose(self) -> None: ...


__all__ = [
    "CalendarAuthError",
    "CalendarError",
    "CalendarNotFound",
    "CalendarPermanentError",
    "CalendarProvider",
    "CalendarQueryError",
    "CalendarSyncPending",
    "EventDraft",
    "EventPatch",
    "EventStatus",
    "ExternalEvent",
    "Interval",
    "SyncResult",
]
