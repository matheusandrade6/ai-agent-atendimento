"""RNF-06: nenhuma aritmetica de data ingenua sobre horario local."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from app.core.time import as_utc, combine_local, day_bounds_utc, now_in, to_tz

SP = "America/Sao_Paulo"


def test_now_in_traz_timezone() -> None:
    moment = now_in(SP)
    assert moment.tzinfo is not None
    assert moment.utcoffset() is not None


def test_datetime_ingenuo_e_recusado() -> None:
    naive = datetime(2026, 9, 9, 14, 0)
    with pytest.raises(ValueError, match="ingenuo"):
        as_utc(naive)
    with pytest.raises(ValueError, match="ingenuo"):
        to_tz(naive, SP)


def test_combine_local_respeita_offset_da_data() -> None:
    # Setembro em Sao Paulo: UTC-3.
    moment = combine_local(date(2026, 9, 9), time(14, 0), SP)
    assert as_utc(moment) == datetime(2026, 9, 9, 17, 0, tzinfo=UTC)


def test_dia_comum_tem_24_horas() -> None:
    start, end = day_bounds_utc(date(2026, 9, 9), SP)
    assert (end - start).total_seconds() == 24 * 3600


def test_dia_de_mudanca_de_offset_nao_assume_24h() -> None:
    """Onde ainda ha horario de verao, o dia da virada tem 23h ou 25h.

    O Brasil nao usa mais DST, entao o caso e exercitado numa timezone que usa —
    o motor de disponibilidade precisa funcionar para qualquer IANA configurada.
    """
    ny = "America/New_York"
    spring_forward = day_bounds_utc(date(2026, 3, 8), ny)  # perde 1h
    fall_back = day_bounds_utc(date(2026, 11, 1), ny)  # ganha 1h

    assert (spring_forward[1] - spring_forward[0]).total_seconds() == 23 * 3600
    assert (fall_back[1] - fall_back[0]).total_seconds() == 25 * 3600


def test_conversao_entre_timezones_preserva_o_instante() -> None:
    moment = combine_local(date(2026, 9, 9), time(14, 0), SP)
    assert to_tz(moment, "UTC") == as_utc(moment)
    assert to_tz(moment, "America/New_York").hour == 13  # UTC-4 em setembro
