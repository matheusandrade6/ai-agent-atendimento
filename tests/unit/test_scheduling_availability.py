"""Motor de disponibilidade: a bateria da 19.1 (Sessao S13).

Horario de verao, virada de dia, buffers, excecoes (feriado), recurso escasso, jornada
partida e servico mais longo que a janela — cada um com o seu teste, todos contra o
algoritmo puro (`compute_slots`). O relogio e congelado (D-36): terca, 08/09/2026, 10h em
Sao Paulo, salvo onde o caso pede outro.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest

from app.calendar.base import CalendarQueryError, CalendarSyncPending, Interval
from app.core.time import combine_local
from app.scheduling.availability import (
    ProviderAgenda,
    ResourceAgenda,
    Slot,
    _CalendarOwner,
    _fetch_busy,
    compute_slots,
)
from app.scheduling.rules import (
    Preferences,
    SchedulingPolicy,
    ServiceDuration,
    WeeklySchedule,
    merge_intervals,
    working_intervals,
)

SP = "America/Sao_Paulo"
NY = "America/New_York"

P1 = uuid.UUID(int=1)
P2 = uuid.UUID(int=2)
R1 = uuid.UUID(int=101)
R2 = uuid.UUID(int=102)

_WEEKDAY = [{"start": "08:00", "end": "12:00"}, {"start": "13:30", "end": "19:00"}]

#: Horario de funcionamento do exemplo da 10.4.
BUSINESS = WeeklySchedule.from_json(
    {
        "weekly": {
            "mon": _WEEKDAY,
            "tue": _WEEKDAY,
            "wed": _WEEKDAY,
            "thu": _WEEKDAY,
            "fri": [{"start": "08:00", "end": "12:00"}, {"start": "13:30", "end": "18:00"}],
            "sat": [{"start": "09:00", "end": "13:00"}],
            "sun": [],
        },
        "exceptions": [
            {"date": "2026-12-25", "closed": True},
            {"date": "2026-12-24", "windows": [{"start": "09:00", "end": "12:00"}]},
        ],
    }
)

FOLLOWS_UNIT = WeeklySchedule(weekly=None)

POLICY = SchedulingPolicy(
    min_lead=timedelta(minutes=120),
    max_horizon=timedelta(days=45),
    granularity=timedelta(minutes=15),
    slots_per_offer=3,
)

TUE = date(2026, 9, 8)
WED = date(2026, 9, 9)
SAT = date(2026, 9, 12)


def local(day: date, hour: int, minute: int = 0, tz: str = SP) -> datetime:
    return combine_local(day, time(hour, minute), tz)


NOW = local(TUE, 10)


def every_day(*windows: tuple[str, str]) -> WeeklySchedule:
    spec = [{"start": start, "end": end} for start, end in windows]
    return WeeklySchedule.from_json(
        {"weekly": {d: spec for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}}
    )


def agenda(
    provider_id: uuid.UUID = P1,
    *,
    tz: str = SP,
    business: WeeklySchedule = BUSINESS,
    working: WeeklySchedule = FOLLOWS_UNIT,
    minutes: int = 30,
    before: int = 0,
    after: int = 0,
    busy: Sequence[Interval] = (),
    resources: Sequence[ResourceAgenda] | None = None,
) -> ProviderAgenda:
    return ProviderAgenda(
        provider_id=provider_id,
        timezone=tz,
        business=business,
        working=working,
        duration=ServiceDuration.resolve(
            duration_minutes=minutes, buffer_before_min=before, buffer_after_min=after
        ),
        busy=busy,
        resources=resources,
    )


def run(
    agendas: Sequence[ProviderAgenda],
    day_from: date,
    day_to: date | None = None,
    *,
    now: datetime = NOW,
    policy: SchedulingPolicy = POLICY,
    preferences: Preferences | None = None,
    limit: int | None = 10_000,
    offset: int = 0,
) -> list[Slot]:
    return compute_slots(
        agendas,
        policy,
        date_from=day_from,
        date_to=day_to or day_from,
        now=now,
        preferences=preferences,
        limit=limit,
        offset=offset,
    )


def hhmm(slots: Sequence[Slot]) -> list[str]:
    return [slot.local_start.strftime("%H:%M") for slot in slots]


def grid(start: str, end: str, step: int = 15) -> list[str]:
    """Horarios de `start` a `end`, inclusive, de `step` em `step` minutos."""
    h, m = (int(x) for x in start.split(":"))
    current = h * 60 + m
    h, m = (int(x) for x in end.split(":"))
    last = h * 60 + m
    out = []
    while current <= last:
        out.append(f"{current // 60:02d}:{current % 60:02d}")
        current += step
    return out


def busy_local(day: date, start: tuple[int, int], end: tuple[int, int], tz: str = SP) -> Interval:
    return Interval(local(day, *start, tz=tz), local(day, *end, tz=tz))


def assert_never_leaves_the_window(
    slots: Sequence[Slot], agendas: Sequence[ProviderAgenda], first: date, last: date
) -> None:
    """Cada bloco (com buffers) cabe inteiro numa janela e nao toca ocupacao."""
    by_id = {a.provider_id: a for a in agendas}
    for slot in slots:
        a = by_id[slot.provider_id]
        windows = merge_intervals(
            w
            for offset in range((last - first).days + 2)
            for w in working_intervals(
                first + timedelta(days=offset), a.business, a.working, a.timezone
            )
        )
        block = a.duration.block_for(slot.start)
        assert any(w.start <= block.start and block.end <= w.end for w in windows), slot
        assert all(b.end <= block.start or block.end <= b.start for b in a.busy), slot
        assert slot.end - slot.start == a.duration.core


# ------------------------------- jornada partida -------------------------------


def test_jornada_partida_nao_atravessa_o_almoco() -> None:
    slots = run([agenda(minutes=60)], WED)
    assert hhmm(slots) == grid("08:00", "11:00") + grid("13:30", "18:00")


def test_lacuna_do_profissional_vira_intervalo() -> None:
    """Almoco declarado no `working_hours` (13.3), cruzado com o horario da unidade."""
    working = WeeklySchedule.from_json(
        {
            "weekly": {
                "wed": [{"start": "07:00", "end": "11:00"}, {"start": "14:00", "end": "20:00"}]
            }
        }
    )
    slots = run([agenda(minutes=60, working=working)], WED)
    assert hhmm(slots) == grid("08:00", "10:00") + grid("14:00", "18:00")


def test_profissional_nao_trabalha_no_dia_ausente_da_grade() -> None:
    working = WeeklySchedule.from_json({"weekly": {"mon": [{"start": "08:00", "end": "19:00"}]}})
    assert run([agenda(working=working)], WED) == []


# ------------------------------ feriados e excecoes ------------------------------

NOW_DEZ = local(date(2026, 12, 20), 10)


def test_feriado_fecha_o_dia() -> None:
    assert run([agenda()], date(2026, 12, 25), now=NOW_DEZ) == []


def test_vespera_com_horario_reduzido() -> None:
    slots = run([agenda(minutes=60)], date(2026, 12, 24), now=NOW_DEZ)
    assert hhmm(slots) == grid("09:00", "11:00")


def test_domingo_fechado() -> None:
    assert run([agenda()], date(2026, 9, 13)) == []


def test_ferias_do_profissional_nao_fecham_a_unidade() -> None:
    on_leave = WeeklySchedule.from_json({"exceptions": [{"date": "2026-09-09", "closed": True}]})
    slots = run([agenda(P1, working=on_leave), agenda(P2)], WED)
    assert slots
    assert {slot.provider_id for slot in slots} == {P2}


# ----------------------------------- buffers -----------------------------------


def test_buffers_ocupam_a_janela_e_a_folga_entre_atendimentos() -> None:
    """30 min + 10 antes + 15 depois, com um atendimento das 10h as 11h.

    - abre 08:00, mas 08:00 nao serve: o buffer de antes cairia as 07:50;
    - 09:15 termina as 09:45 + 15 de buffer = 10:00, encostando no ocupado: serve;
    - 09:30 nao: o buffer de depois invade o ocupado;
    - 11:15 e o primeiro depois: o buffer de antes comeca as 11:05;
    - 11:15 e tambem o ultimo da manha: 11:45 + 15 = 12:00.
    """
    a = agenda(before=10, after=15, busy=[busy_local(WED, (10, 0), (11, 0))])
    slots = run([a], WED)
    morning = [h for h in hhmm(slots) if h < "12:00"]
    assert morning == ["08:15", "08:30", "08:45", "09:00", "09:15", "11:15"]
    afternoon = [h for h in hhmm(slots) if h >= "12:00"]
    assert afternoon == grid("13:45", "18:15")
    assert all(slot.end - slot.start == timedelta(minutes=30) for slot in slots)
    assert_never_leaves_the_window(slots, [a], WED, WED)


def test_duracao_por_profissional() -> None:
    standard = agenda(P1, minutes=30)
    slow = ProviderAgenda(
        provider_id=P2,
        timezone=SP,
        business=BUSINESS,
        working=FOLLOWS_UNIT,
        duration=ServiceDuration.resolve(duration_minutes=30, override_minutes=90),
    )
    assert hhmm(run([slow], WED))[-1] == "17:30"
    assert hhmm(run([standard], WED))[-1] == "18:30"


# ------------------------- servico mais longo que a janela -------------------------


def test_servico_mais_longo_que_qualquer_janela_retorna_vazio() -> None:
    """6h nao cabem em nenhuma janela da semana (a maior e 13:30-19:00)."""
    assert run([agenda(minutes=360)], WED, WED + timedelta(days=7)) == []


def test_servico_do_tamanho_exato_da_janela_cabe_uma_vez() -> None:
    slots = run([agenda(minutes=240)], SAT)
    assert hhmm(slots) == ["09:00"]
    assert slots[0].local_end.strftime("%H:%M") == "13:00"


def test_buffer_que_faz_o_servico_passar_da_janela_retorna_vazio() -> None:
    assert run([agenda(minutes=240, after=5)], SAT) == []
    assert run([agenda(minutes=240, before=5)], SAT) == []


@pytest.mark.parametrize("minutes", [15, 50, 95, 240, 330, 400])
@pytest.mark.parametrize(("before", "after"), [(0, 0), (10, 5), (30, 30)])
def test_nenhum_slot_estoura_a_janela(minutes: int, before: int, after: int) -> None:
    start, end = WED, WED + timedelta(days=13)
    busy = [
        busy_local(WED, (9, 10), (9, 40)),
        busy_local(date(2026, 9, 10), (14, 0), (16, 20)),
        busy_local(SAT, (11, 0), (11, 5)),
    ]
    a = agenda(minutes=minutes, before=before, after=after, busy=busy)
    slots = run([a], start, end)
    assert_never_leaves_the_window(slots, [a], start, end)
    if minutes + before + after > 330:
        assert slots == []


# --------------------------- antecedencia e horizonte ---------------------------


def test_antecedencia_minima() -> None:
    """10h com 2h de antecedencia: 11:30 (que cabe antes do almoco) nao entra."""
    slots = run([agenda()], TUE)
    assert hhmm(slots)[0] == "13:30"


def test_antecedencia_e_inclusiva() -> None:
    slots = run([agenda()], TUE, now=local(TUE, 9))
    assert hhmm(slots)[0] == "11:00"


def test_horizonte_maximo() -> None:
    policy = SchedulingPolicy(
        min_lead=timedelta(0),
        max_horizon=timedelta(days=1),
        granularity=timedelta(minutes=15),
        slots_per_offer=3,
    )
    slots = run([agenda()], TUE, date(2027, 9, 8), policy=policy)
    assert slots[-1].local_start == local(WED, 10)


# ------------------------------- horario de verao -------------------------------

NIGHT = every_day(("00:00", "06:00"))
DST_POLICY = SchedulingPolicy(
    min_lead=timedelta(0),
    max_horizon=timedelta(days=45),
    granularity=timedelta(minutes=60),
    slots_per_offer=3,
)


def test_salto_do_horario_de_verao_nao_gera_hora_inexistente() -> None:
    """08/03/2026, Nova York: 02:00 vira 03:00. A janela 00-06 tem 5h de verdade."""
    day = date(2026, 3, 8)
    a = agenda(tz=NY, business=NIGHT, minutes=60)
    slots = run([a], day, now=local(date(2026, 3, 7), 12, tz=NY), policy=DST_POLICY)
    assert hhmm(slots) == ["00:00", "01:00", "03:00", "04:00", "05:00"]
    assert [s.start.hour for s in slots] == [5, 6, 7, 8, 9]  # UTC continuo
    assert_never_leaves_the_window(slots, [a], day, day)


def test_salto_com_passo_menor_que_a_hora() -> None:
    day = date(2026, 3, 8)
    policy = SchedulingPolicy(
        min_lead=timedelta(0),
        max_horizon=timedelta(days=45),
        granularity=timedelta(minutes=30),
        slots_per_offer=3,
    )
    slots = run(
        [agenda(tz=NY, business=NIGHT, minutes=60)],
        day,
        now=local(date(2026, 3, 7), 12, tz=NY),
        policy=policy,
    )
    assert len(slots) == 9  # 5h reais, 60 min, passo de 30
    assert all(slot.local_start.hour != 2 for slot in slots)


def test_recuo_do_horario_de_verao_oferece_a_hora_repetida_duas_vezes() -> None:
    """01/11/2026, Nova York: 01:00-02:00 acontece duas vezes; a janela 00-06 tem 7h."""
    day = date(2026, 11, 1)
    a = agenda(tz=NY, business=NIGHT, minutes=60)
    slots = run([a], day, now=local(date(2026, 10, 31), 12, tz=NY), policy=DST_POLICY)
    assert hhmm(slots) == ["00:00", "01:00", "01:00", "02:00", "03:00", "04:00", "05:00"]
    assert len({slot.start for slot in slots}) == 7  # instantes distintos, sem dedup errado
    assert_never_leaves_the_window(slots, [a], day, day)


def test_inicio_do_horario_de_verao_em_sao_paulo_pulava_a_meia_noite() -> None:
    """04/11/2018: 00:00 virava 01:00. A janela 00-04 teve 3h, a partir das 01:00."""
    day = date(2018, 11, 4)
    a = agenda(business=every_day(("00:00", "04:00")), minutes=60)
    slots = run([a], day, now=local(date(2018, 11, 1), 12), policy=DST_POLICY)
    assert hhmm(slots) == ["01:00", "02:00", "03:00"]


def test_fim_do_horario_de_verao_em_sao_paulo_repetia_as_23h() -> None:
    """17/02/2019: a meia-noite voltava para 23:00 do dia 16. A janela 22-24 teve 3h."""
    day = date(2019, 2, 16)
    a = agenda(business=every_day(("22:00", "24:00")), minutes=60)
    slots = run([a], day, now=local(date(2019, 2, 14), 12), policy=DST_POLICY)
    assert hhmm(slots) == ["22:00", "23:00", "23:00"]
    assert [slot.local_start.utcoffset() for slot in slots] == [
        timedelta(hours=-2),
        timedelta(hours=-2),
        timedelta(hours=-3),
    ]


def test_horario_de_verao_muda_o_utc_do_mesmo_horario_local() -> None:
    """09:00 em Nova York e 14:00Z antes do salto e 13:00Z depois — persistencia em UTC."""
    business = every_day(("09:00", "10:00"))
    a = agenda(tz=NY, business=business, minutes=60)
    slots = run(
        [a],
        date(2026, 3, 7),
        date(2026, 3, 9),
        now=local(date(2026, 3, 1), 12, tz=NY),
        policy=DST_POLICY,
    )
    assert [s.start for s in slots] == [
        datetime(2026, 3, 7, 14, tzinfo=UTC),
        datetime(2026, 3, 8, 13, tzinfo=UTC),
        datetime(2026, 3, 9, 13, tzinfo=UTC),
    ]
    assert hhmm(slots) == ["09:00", "09:00", "09:00"]


# ---------------------------------- virada de dia ----------------------------------

MON = date(2026, 9, 7)
ALL_DAY = every_day(("00:00", "24:00"))
HOURLY = SchedulingPolicy(
    min_lead=timedelta(minutes=120),
    max_horizon=timedelta(days=45),
    granularity=timedelta(minutes=60),
    slots_per_offer=3,
)


def test_antecedencia_atravessa_a_meia_noite() -> None:
    slots = run([agenda(business=ALL_DAY, minutes=60)], MON, TUE, now=local(MON, 23), policy=HOURLY)
    assert slots[0].local_start == local(TUE, 1)
    assert all(slot.local_start.date() == TUE for slot in slots)


def test_dia_da_consulta_e_o_dia_local_nao_o_utc() -> None:
    """22h em Sao Paulo ja e o dia seguinte em UTC; continua sendo slot de segunda."""
    a = agenda(business=every_day(("20:00", "24:00")), minutes=60)
    slots = run([a], MON, now=local(MON, 8), policy=HOURLY)
    assert hhmm(slots) == ["20:00", "21:00", "22:00", "23:00"]
    assert all(slot.local_start.date() == MON for slot in slots)
    assert slots[-1].start == datetime(2026, 9, 8, 2, tzinfo=UTC)


def test_ocupacao_que_atravessa_a_meia_noite() -> None:
    busy = Interval(local(MON, 23, 30), local(TUE, 0, 30))
    a = agenda(business=ALL_DAY, minutes=60, busy=[busy])
    starts = {s.local_start for s in run([a], MON, TUE, now=local(MON, 8), policy=HOURLY)}
    assert local(MON, 22) in starts
    assert local(MON, 23) not in starts
    assert local(TUE, 0) not in starts
    assert local(TUE, 1) in starts


def test_jornada_noturna_continua_atravessando_a_meia_noite() -> None:
    """Turno 22h-02h declarado em dois pedacos: um atendimento de 3h cabe as 22h e as 23h."""
    night_shift = every_day(("00:00", "02:00"), ("22:00", "24:00"))
    a = agenda(business=night_shift, minutes=180)
    slots = run([a], MON, now=local(MON, 8), policy=HOURLY)
    assert [s.local_start for s in slots] == [local(MON, 22), local(MON, 23)]
    assert slots[-1].local_end == local(TUE, 2)


# ---------------------------------- recurso escasso ----------------------------------

AT_14 = busy_local(WED, (14, 0), (15, 0))
HALF_HOUR = SchedulingPolicy(
    min_lead=timedelta(minutes=120),
    max_horizon=timedelta(days=45),
    granularity=timedelta(minutes=30),
    slots_per_offer=3,
)


def _wed(agendas: Sequence[ProviderAgenda]) -> dict[str, Slot]:
    return {
        slot.local_start.strftime("%H:%M"): slot for slot in run(agendas, WED, policy=HALF_HOUR)
    }


def test_sala_unica_ocupada_bloqueia_todos_os_profissionais() -> None:
    room = [ResourceAgenda(R1, capacity=1, bookings=[AT_14])]
    slots = _wed([agenda(P1, minutes=60, resources=room), agenda(P2, minutes=60, resources=room)])
    assert "13:30" not in slots  # 13:30-14:30 invade
    assert "14:00" not in slots
    assert "14:30" not in slots
    assert slots["15:00"].resource_id == R1


def test_capacidade_maior_que_um_comporta_uso_simultaneo() -> None:
    room = [ResourceAgenda(R1, capacity=2, bookings=[AT_14])]
    assert _wed([agenda(minutes=60, resources=room)])["14:00"].resource_id == R1


def test_segundo_recurso_assume_quando_o_primeiro_esta_ocupado() -> None:
    rooms = [ResourceAgenda(R1, capacity=1, bookings=[AT_14]), ResourceAgenda(R2, capacity=1)]
    slots = _wed([agenda(minutes=60, resources=rooms)])
    assert slots["14:00"].resource_id == R2
    assert slots["10:00"].resource_id == R1


def test_recurso_bloqueado_no_calendario_bloqueia_qualquer_capacidade() -> None:
    room = [ResourceAgenda(R1, capacity=3, blocked=[AT_14])]
    assert "14:00" not in _wed([agenda(minutes=60, resources=room)])


def test_profissional_ocupado_com_recurso_livre_nao_tem_slot() -> None:
    room = [ResourceAgenda(R1, capacity=1)]
    assert "14:00" not in _wed([agenda(minutes=60, busy=[AT_14], resources=room)])


def test_servico_que_exige_recurso_sem_nenhum_disponivel_nao_tem_slot() -> None:
    assert run([agenda(resources=())], WED) == []


def test_recurso_respeita_os_buffers_do_bloco() -> None:
    """A sala tambem precisa estar livre durante o preparo e a limpeza."""
    room = [ResourceAgenda(R1, capacity=1, bookings=[AT_14])]
    slots = _wed([agenda(minutes=30, after=30, resources=room)])
    assert "13:30" not in slots  # 13:30-14:00 + 30 de limpeza invade a sala
    assert "13:00" not in slots  # fora da janela (almoco), so para registrar
    assert "15:00" in slots


# ------------------------------ uniao, ordem e corte ------------------------------


def test_uniao_de_profissionais_deduplica_por_horario() -> None:
    slots = run([agenda(P1), agenda(P2)], WED)
    starts = [slot.start for slot in slots]
    assert len(starts) == len(set(starts))
    assert {slot.provider_id for slot in slots} == {P1}  # empate: ordem das agendas


def test_horario_livre_so_em_um_profissional_vem_dele() -> None:
    p1 = agenda(P1, busy=[AT_14])
    slots = {s.local_start.strftime("%H:%M"): s for s in run([p1, agenda(P2)], WED)}
    assert slots["14:00"].provider_id == P2
    assert slots["10:00"].provider_id == P1


def test_preferencia_ordena_antes_da_proximidade() -> None:
    prefs = Preferences(periods=frozenset({"afternoon"}))
    slots = run([agenda()], WED, preferences=prefs, limit=3)
    assert hhmm(slots) == ["13:30", "13:45", "14:00"]


def test_preferencia_impossivel_nao_zera_a_oferta() -> None:
    prefs = Preferences(weekdays=frozenset({"sun"}))  # domingo a unidade nao abre
    slots = run([agenda()], WED, WED + timedelta(days=7), preferences=prefs, limit=3)
    assert hhmm(slots) == ["08:00", "08:15", "08:30"]


def test_proximidade_de_um_instante_de_referencia() -> None:
    friday = date(2026, 9, 11)
    prefs = Preferences(around=local(friday, 15))
    slots = run([agenda()], WED, friday, preferences=prefs, limit=3)
    assert slots[0].local_start == local(friday, 15)
    assert {s.local_start for s in slots} <= {
        local(friday, 14, 45),
        local(friday, 15),
        local(friday, 15, 15),
    }


def test_limite_padrao_e_slots_per_offer_e_offset_pagina() -> None:
    first = run([agenda()], WED, limit=None)
    second = run([agenda()], WED, limit=None, offset=3)
    assert hhmm(first) == ["08:00", "08:15", "08:30"]
    assert hhmm(second) == ["08:45", "09:00", "09:15"]


def test_now_ingenuo_e_recusado() -> None:
    with pytest.raises(ValueError, match="ingenuo"):
        run([agenda()], WED, now=datetime(2026, 9, 8, 10, 0))


def test_slot_sai_em_hora_local_para_a_tool() -> None:
    slot = run([agenda()], WED, limit=1)[0]
    assert slot.as_dict() == {
        "start": "2026-09-09T08:00:00-03:00",
        "end": "2026-09-09T08:30:00-03:00",
        "provider_id": str(P1),
        "resource_id": None,
    }


# ------------------------- calendario externo: falha fechado -------------------------


class _Calendar:
    def __init__(self, result: dict[str, list[Interval]] | Exception) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    async def get_busy(
        self, calendar_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, list[Interval]]:
        self.calls.append(calendar_ids)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def __getattr__(self, name: str) -> Any:  # o resto do Protocol nao e usado aqui
        raise AttributeError(name)


START = datetime(2026, 9, 9, 0, tzinfo=UTC)
END = START + timedelta(days=1)


async def test_freebusy_agrupa_por_cliente() -> None:
    busy = [Interval(START, START + timedelta(hours=1))]
    calendar = _Calendar({"a@x": busy, "b@x": []})
    owners = [_CalendarOwner(P1, "a@x", calendar), _CalendarOwner(P2, "b@x", calendar)]  # type: ignore[arg-type]
    result, failed = await _fetch_busy(owners, START, END)
    assert calendar.calls == [["a@x", "b@x"]]
    assert result == {P1: busy, P2: []}
    assert failed == {}


async def test_calendario_que_nao_respondeu_fica_de_fora_e_nao_livre() -> None:
    error = CalendarQueryError(failed={"b@x": "notFound"}, partial={"a@x": []})
    calendar = _Calendar(error)
    owners = [_CalendarOwner(P1, "a@x", calendar), _CalendarOwner(P2, "b@x", calendar)]  # type: ignore[arg-type]
    result, failed = await _fetch_busy(owners, START, END)
    assert result == {P1: []}
    assert failed == {P2: "notFound"}


async def test_falha_transitoria_tira_o_lote_inteiro() -> None:
    calendar = _Calendar(CalendarSyncPending("google fora", attempts=5))
    owners = [_CalendarOwner(P1, "a@x", calendar)]  # type: ignore[arg-type]
    result, failed = await _fetch_busy(owners, START, END)
    assert result == {}
    assert set(failed) == {P1}


async def test_agenda_propria_nao_consulta_calendario_e_sem_credencial_falha() -> None:
    owners = [_CalendarOwner(P1, None, None), _CalendarOwner(P2, "b@x", None)]
    result, failed = await _fetch_busy(owners, START, END)
    assert result == {P1: []}
    assert set(failed) == {P2}
