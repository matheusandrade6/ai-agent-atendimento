"""Desfecho de sincronizacao do agendamento (14.3, "Resiliencia").

A regra sob teste: falha de calendario **nunca** derruba nem desfaz o agendamento — ela
muda `sync_status`. Falha transitoria persistente vira `pending_sync` (retentavel);
falha que retentar nao resolve vira `sync_failed` (precisa de humano).

A sessao e falsa: aqui so interessa *que estado* e gravado. O SQL contra Postgres real
fica em `tests/integration/test_calendar_sync_state.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.sql.dml import Update

from app.calendar.base import (
    CalendarAuthError,
    CalendarPermanentError,
    CalendarSyncPending,
    EventDraft,
    EventPatch,
    ExternalEvent,
    Interval,
    SyncResult,
)
from app.calendar.sync_state import push_appointment_event

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
DRAFT = EventDraft(summary="Consulta — Rex", start=T0, end=T0 + timedelta(hours=1), timezone="UTC")


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[Update] = []

    async def execute(self, statement: Update) -> None:
        self.statements.append(statement)

    def written(self) -> dict[str, Any]:
        assert len(self.statements) == 1, "deveria gravar exatamente uma vez"
        return dict(self.statements[0].compile().params)


class FakeProvider:
    def __init__(self, outcome: ExternalEvent | Exception) -> None:
        self._outcome = outcome
        self.calls: list[tuple[str, str]] = []

    async def create_event(
        self, calendar_id: str, event: EventDraft, idempotency_key: str
    ) -> ExternalEvent:
        self.calls.append((calendar_id, idempotency_key))
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    async def get_busy(
        self, calendar_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, list[Interval]]:
        raise NotImplementedError

    async def update_event(
        self, calendar_id: str, event_id: str, patch: EventPatch
    ) -> ExternalEvent:
        raise NotImplementedError

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        raise NotImplementedError

    async def sync_changes(self, calendar_id: str, sync_token: str | None) -> SyncResult:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


async def _push(provider: FakeProvider, session: RecordingSession, appt: uuid.UUID) -> Any:
    return await push_appointment_event(
        session,  # type: ignore[arg-type]
        provider,
        appointment_id=appt,
        calendar_id="cal-1",
        draft=DRAFT,
    )


async def test_sucesso_grava_synced_e_o_id_do_evento() -> None:
    appt = uuid.uuid4()
    provider = FakeProvider(ExternalEvent(id="ev-9", status="confirmed"))
    session = RecordingSession()

    status, event = await _push(provider, session, appt)

    assert status == "synced"
    assert event is not None
    assert provider.calls == [("cal-1", str(appt))]  # iCalUID = appointment_id
    written = session.written()
    assert written["sync_status"] == "synced"
    assert written["external_event_id"] == "ev-9"


async def test_falha_transitoria_persistente_deixa_pending_sync_sem_propagar() -> None:
    provider = FakeProvider(CalendarSyncPending("Google fora do ar", attempts=5))
    session = RecordingSession()

    status, event = await _push(provider, session, uuid.uuid4())

    assert (status, event) == ("pending_sync", None)
    written = session.written()
    assert written["sync_status"] == "pending_sync"
    assert "external_event_id" not in written  # nao apaga nem inventa id


@pytest.mark.parametrize(
    "error", [CalendarAuthError("invalid_grant"), CalendarPermanentError("400", status_code=400)]
)
async def test_falha_que_retentar_nao_resolve_vira_sync_failed(error: Exception) -> None:
    session = RecordingSession()

    status, event = await _push(FakeProvider(error), session, uuid.uuid4())

    assert (status, event) == ("sync_failed", None)
    assert session.written()["sync_status"] == "sync_failed"
