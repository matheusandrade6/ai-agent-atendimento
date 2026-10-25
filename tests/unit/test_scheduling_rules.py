"""Regras da 13.3, isoladas do motor (Sessao S13)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from pydantic import ValidationError

from app.calendar.base import Interval
from app.domain.tenant_config import BusinessHours, SchedulingRules
from app.scheduling.rules import (
    Preferences,
    SchedulingPolicy,
    ServiceDuration,
    WeeklySchedule,
    fits_capacity,
    intersect_windows,
    is_free,
    local_to_utc,
    merge_intervals,
    uses_location,
    working_intervals,
)

SP = "America/Sao_Paulo"
NY = "America/New_York"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def iv(start: datetime, minutes: int) -> Interval:
    return Interval(start, start + timedelta(minutes=minutes))


# ------------------------------- politica -------------------------------


def test_politica_vem_da_config() -> None:
    policy = SchedulingPolicy.from_rules(
        SchedulingRules(min_lead_time_minutes=90, max_horizon_days=10, slot_granularity_minutes=20)
    )
    assert policy.min_lead == timedelta(minutes=90)
    assert policy.max_horizon == timedelta(days=10)
    assert policy.granularity == timedelta(minutes=20)


def test_antecedencia_e_horizonte_sao_inclusivos() -> None:
    policy = SchedulingPolicy.from_rules(
        SchedulingRules(min_lead_time_minutes=120, max_horizon_days=2)
    )
    now = utc(2026, 9, 8, 13)
    assert policy.accepts(now + timedelta(hours=2), now)
    assert not policy.accepts(now + timedelta(hours=2) - timedelta(minutes=1), now)
    assert policy.accepts(now + timedelta(days=2), now)
    assert not policy.accepts(now + timedelta(days=2, minutes=1), now)


# -------------------------------- duracao --------------------------------


def test_duracao_do_profissional_vence_a_do_servico() -> None:
    duration = ServiceDuration.resolve(duration_minutes=30, override_minutes=45)
    assert duration.core == timedelta(minutes=45)


def test_bloco_inclui_buffers_mas_atendimento_nao() -> None:
    duration = ServiceDuration.resolve(
        duration_minutes=30, buffer_before_min=10, buffer_after_min=15
    )
    start = utc(2026, 9, 8, 12)
    assert duration.block_for(start) == Interval(
        start - timedelta(minutes=10), start + timedelta(minutes=45)
    )
    assert duration.total == timedelta(minutes=55)


@pytest.mark.parametrize(
    ("kwargs"),
    [
        {"duration_minutes": 0},
        {"duration_minutes": 30, "override_minutes": 0},
        {"duration_minutes": 30, "buffer_before_min": -5},
    ],
)
def test_duracao_invalida_e_recusada(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        ServiceDuration.resolve(**kwargs)


# ------------------------------- grade semanal -------------------------------


def test_working_hours_vazio_segue_a_unidade() -> None:
    schedule = WeeklySchedule.from_json({})
    assert schedule.weekly is None
    assert schedule.windows_on(date(2026, 9, 8)) is None


def test_working_hours_com_grade_restringe_dia_ausente() -> None:
    schedule = WeeklySchedule.from_json({"weekly": {"mon": [{"start": "09:00", "end": "12:00"}]}})
    assert schedule.windows_on(date(2026, 9, 7)) == ((540, 720),)  # segunda
    assert schedule.windows_on(date(2026, 9, 8)) == ()  # terca: nao trabalha


def test_excecao_substitui_o_dia() -> None:
    schedule = WeeklySchedule.from_json(
        {
            "weekly": {"thu": [{"start": "08:00", "end": "18:00"}]},
            "exceptions": [
                {"date": "2026-12-24", "windows": [{"start": "09:00", "end": "12:00"}]},
                {"date": "2026-12-31", "closed": True},
            ],
        }
    )
    assert schedule.windows_on(date(2026, 12, 24)) == ((540, 720),)
    assert schedule.windows_on(date(2026, 12, 31)) == ()


def test_excecao_vale_mesmo_sem_grade_semanal() -> None:
    """Ferias do profissional que segue a unidade: `{exceptions: [...]}` sem `weekly`."""
    schedule = WeeklySchedule.from_json({"exceptions": [{"date": "2026-10-12", "closed": True}]})
    assert schedule.windows_on(date(2026, 10, 12)) == ()
    assert schedule.windows_on(date(2026, 10, 13)) is None


def test_24h_e_fim_do_dia() -> None:
    schedule = WeeklySchedule.from_json({"weekly": {"mon": [{"start": "20:00", "end": "24:00"}]}})
    assert schedule.windows_on(date(2026, 9, 7)) == ((1200, 1440),)


@pytest.mark.parametrize("bad", ["24:30", "25:00"])
def test_hora_impossivel_e_recusada(bad: str) -> None:
    with pytest.raises(ValueError):
        WeeklySchedule.from_json({"weekly": {"mon": [{"start": "08:00", "end": bad}]}})


def test_24h_nao_vale_como_inicio() -> None:
    with pytest.raises((ValueError, ValidationError)):
        WeeklySchedule.from_json({"weekly": {"mon": [{"start": "24:00", "end": "24:00"}]}})


def test_intersecao_de_janelas() -> None:
    business = ((480, 720), (810, 1140))  # 08-12, 13:30-19
    working = ((540, 1020),)  # 09-17
    assert intersect_windows(business, working) == ((540, 720), (810, 1020))
    assert intersect_windows(business, ()) == ()


def test_intervalos_do_dia_cruzam_unidade_e_profissional() -> None:
    business = WeeklySchedule.from_business_hours(
        BusinessHours.model_validate(
            {
                "weekly": {
                    "tue": [{"start": "08:00", "end": "12:00"}, {"start": "13:30", "end": "19:00"}]
                }
            }
        )
    )
    working = WeeklySchedule.from_json({"weekly": {"tue": [{"start": "10:00", "end": "15:00"}]}})
    assert working_intervals(date(2026, 9, 8), business, working, SP) == [
        Interval(utc(2026, 9, 8, 13), utc(2026, 9, 8, 15)),
        Interval(utc(2026, 9, 8, 16, 30), utc(2026, 9, 8, 18)),
    ]


def test_sem_grade_nenhuma_nao_ha_jornada() -> None:
    open_ended = WeeklySchedule(weekly=None)
    assert working_intervals(date(2026, 9, 8), open_ended, open_ended, SP) == []


# --------------------------- horario de verao (bordas) ---------------------------


def test_hora_que_nao_existe_vira_o_instante_do_salto() -> None:
    # 08/03/2026, Nova York: 02:00 EST vira 03:00 EDT (07:00Z).
    assert local_to_utc(date(2026, 3, 8), 150, NY) == utc(2026, 3, 8, 7)


def test_hora_repetida_pega_a_primeira_ocorrencia() -> None:
    # 01/11/2026, Nova York: 01:30 acontece em EDT (05:30Z) e de novo em EST (06:30Z).
    assert local_to_utc(date(2026, 11, 1), 90, NY) == utc(2026, 11, 1, 5, 30)


def test_meia_noite_que_nao_existiu_em_sao_paulo() -> None:
    """04/11/2018: o horario de verao brasileiro comecava a 00:00 — a meia-noite pulava."""
    assert local_to_utc(date(2018, 11, 3), 1440, SP) == utc(2018, 11, 4, 3)


# --------------------------------- ocupacao ---------------------------------


def test_merge_une_sobrepostos_e_encostados() -> None:
    base = utc(2026, 9, 8, 12)
    merged = merge_intervals([iv(base, 30), iv(base + timedelta(minutes=30), 30), iv(base, 10)])
    assert merged == [iv(base, 60)]


def test_livre_quando_so_encosta() -> None:
    base = utc(2026, 9, 8, 12)
    busy = merge_intervals([iv(base, 60)])
    starts = [b.start for b in busy]
    assert is_free(iv(base + timedelta(minutes=60), 30), busy, starts)
    assert is_free(iv(base - timedelta(minutes=30), 30), busy, starts)
    assert not is_free(iv(base + timedelta(minutes=59), 30), busy, starts)
    assert not is_free(iv(base - timedelta(minutes=10), 90), busy, starts)


def test_capacidade_conta_pico_e_nao_total() -> None:
    base = utc(2026, 9, 8, 12)
    in_sequence = [iv(base, 30), iv(base + timedelta(minutes=30), 30)]
    assert not fits_capacity(iv(base, 60), in_sequence, capacity=1)
    assert fits_capacity(iv(base, 60), in_sequence, capacity=2)
    concurrent = [iv(base, 60), iv(base, 60)]
    assert not fits_capacity(iv(base, 30), concurrent, capacity=2)
    assert fits_capacity(iv(base, 30), concurrent, capacity=3)
    assert not fits_capacity(iv(base, 30), [], capacity=0)


# ------------------------------ preferencias e modalidade ------------------------------


def test_aderencia_conta_preferencias_atendidas() -> None:
    prefs = Preferences(periods=frozenset({"afternoon"}), weekdays=frozenset({"tue"}))
    tuesday_afternoon = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    tuesday_morning = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    wednesday_morning = datetime(2026, 9, 9, 9, 0, tzinfo=UTC)
    assert prefs.adherence(tuesday_afternoon) == 2
    assert prefs.adherence(tuesday_morning) == 1
    assert prefs.adherence(wednesday_morning) == 0


def test_aderencia_por_faixa_de_horario() -> None:
    prefs = Preferences(not_before=time(10, 0), not_after=time(16, 0))
    assert prefs.adherence(datetime(2026, 9, 8, 12, 0, tzinfo=UTC)) == 2
    assert prefs.adherence(datetime(2026, 9, 8, 17, 0, tzinfo=UTC)) == 1


def test_servico_online_ignora_location() -> None:
    assert uses_location("in_person")
    assert not uses_location("online")
