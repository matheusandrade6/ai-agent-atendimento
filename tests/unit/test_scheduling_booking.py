"""Partes puras do booking (13.4; Sessao S14). O resto e testado contra Postgres."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.calendar.base import Interval
from app.scheduling.booking import (
    ConfirmResult,
    HoldResult,
    _is_overlap_violation,
    _violated_constraint,
    idempotency_key_for,
    lock_keys,
)
from app.workers.cron import expire_holds_job

P = uuid.UUID(int=1)
R = uuid.UUID(int=2)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_chave_de_idempotencia_e_estavel_e_distinta_por_hold() -> None:
    hold = uuid.UUID("6f1c2d3e-0000-4000-8000-000000000001")
    assert idempotency_key_for(hold) == idempotency_key_for(uuid.UUID(str(hold)))
    assert idempotency_key_for(hold) != idempotency_key_for(uuid.uuid4())
    assert len(idempotency_key_for(hold)) == 64


def test_lock_por_profissional_e_dia() -> None:
    block = Interval(utc(2026, 11, 11, 17), utc(2026, 11, 11, 17, 40))
    assert lock_keys([("provider", P)], block) == [f"provider:{P}:2026-11-11"]


def test_lock_inclui_recurso() -> None:
    block = Interval(utc(2026, 11, 11, 17), utc(2026, 11, 11, 18))
    assert lock_keys([("provider", P), ("resource", R)], block) == [
        f"provider:{P}:2026-11-11",
        f"resource:{R}:2026-11-11",
    ]


def test_bloco_que_atravessa_a_meia_noite_trava_os_dois_dias() -> None:
    """Dois blocos sobrepostos sempre compartilham uma chave, mesmo na virada do dia."""
    late = Interval(utc(2026, 11, 11, 23, 30), utc(2026, 11, 12, 0, 30))
    early = Interval(utc(2026, 11, 12, 0), utc(2026, 11, 12, 1))
    assert set(lock_keys([("provider", P)], late)) & set(lock_keys([("provider", P)], early))
    assert len(lock_keys([("provider", P)], late)) == 2


def test_bloco_que_termina_a_meia_noite_nao_trava_o_dia_seguinte() -> None:
    block = Interval(utc(2026, 11, 11, 23), utc(2026, 11, 12, 0))
    assert lock_keys([("provider", P)], block) == [f"provider:{P}:2026-11-11"]


def test_lock_usa_o_dia_utc_do_instante() -> None:
    # 22:00 do dia 11 em Sao Paulo e 01:00Z do dia 12: a chave segue o UTC.
    block = Interval(utc(2026, 11, 12, 1), utc(2026, 11, 12, 1, 30))
    assert lock_keys([("provider", P)], block) == [f"provider:{P}:2026-11-12"]


def test_saida_das_tools() -> None:
    assert HoldResult(status="slot_taken").as_dict() == {"status": "slot_taken"}
    assert ConfirmResult(status="slot_taken").as_dict() == {"status": "slot_taken"}
    hold = uuid.uuid4()
    start = utc(2026, 11, 11, 17)
    payload = HoldResult(
        status="held",
        hold_id=hold,
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        expires_at=start - timedelta(hours=1),
    ).as_dict()
    assert payload == {
        "status": "held",
        "hold_id": str(hold),
        "starts_at": "2026-11-11T17:00:00+00:00",
        "ends_at": "2026-11-11T17:30:00+00:00",
        "expires_at": "2026-11-11T16:00:00+00:00",
    }


class _AsyncpgError(Exception):
    def __init__(self, message: str, *, constraint: str | None, sqlstate: str) -> None:
        super().__init__(message)
        self.constraint_name = constraint
        self.sqlstate = sqlstate


def _integrity(cause: Exception, message: str = "integrity") -> IntegrityError:
    adapted = Exception(message)
    adapted.__cause__ = cause
    return IntegrityError("INSERT", {}, adapted)


def test_violacao_da_exclusao_e_reconhecida_pelo_nome_ou_pelo_sqlstate() -> None:
    by_name = _integrity(_AsyncpgError("x", constraint="appt_no_overlap", sqlstate="23P01"))
    assert _is_overlap_violation(by_name)
    by_state = _integrity(_AsyncpgError("x", constraint=None, sqlstate="23P01"))
    assert _is_overlap_violation(by_state)
    by_message = IntegrityError(
        "INSERT", {}, Exception('violates exclusion constraint "appt_no_overlap"')
    )
    assert _is_overlap_violation(by_message)


def test_outra_violacao_nao_vira_slot_taken() -> None:
    unique = _integrity(
        _AsyncpgError("x", constraint="uq_appointment_idempotency", sqlstate="23505")
    )
    assert not _is_overlap_violation(unique)
    assert _violated_constraint(unique) == "uq_appointment_idempotency"
    fk = _integrity(_AsyncpgError("x", constraint="appointments_contact_id_fkey", sqlstate="23503"))
    assert not _is_overlap_violation(fk)


async def test_job_de_expiracao_usa_sessao_que_atravessa_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    class _Session:
        pass

    class _Bypass:
        async def __aenter__(self) -> _Session:
            return _Session()

        async def __aexit__(self, *exc: object) -> None:
            return None

    async def fake_expire(session: object, now: datetime) -> int:
        calls.append((session, now))
        return 3

    monkeypatch.setattr("app.workers.cron.bypass_rls_session", lambda settings=None: _Bypass())
    monkeypatch.setattr("app.workers.cron.expire_holds", fake_expire)

    assert await expire_holds_job({}) == 3
    ((session, now),) = calls
    assert isinstance(session, _Session)
    assert now.tzinfo is not None
