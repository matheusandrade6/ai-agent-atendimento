"""Remarcar e cancelar no dominio, sem a tool (secao 12.2, 12.3, 13.4; S16).

O que a tool nao mostra e o que este arquivo protege: a troca antigo-por-novo e atomica, o
horario vizinho nao conflita com o proprio agendamento, o recurso escasso vale tambem para
quem remarca, e — sob concorrencia — ninguem fica com dois agendamentos nem com nenhum.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, time, timedelta

import pytest
import sqlalchemy as sa

from app.core.config import Settings
from app.core.db import dispose_engine, tenant_session
from app.core.time import combine_local
from app.domain.tenant_config import TenantConfig, load_tenant_config
from app.scheduling import booking
from app.scheduling.booking import CancelResult, RescheduleResult
from app.scheduling.rules import ChangeKind, ChangePolicy
from tests.conftest import TENANTS_DIR
from tests.integration.booking_seed import BookingSeed, drop_tenant, seed_booking
from tests.integration.change_support import (
    EDGE,
    FAR,
    NEAR,
    NOW,
    SP,
    WED,
    active,
    another_contact,
    appt,
    count,
    insert_appointment,
    rows,
)

pytestmark = pytest.mark.integration

EIGHT = combine_local(WED, time(8, 0), SP)
EIGHT_FIFTEEN = EIGHT + timedelta(minutes=15)
NINE = combine_local(WED, time(9, 0), SP)


@pytest.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    await dispose_engine()
    yield
    await dispose_engine()


@pytest.fixture
def config() -> TenantConfig:
    return load_tenant_config(TENANTS_DIR / "clinica-exemplo.yaml")


@pytest.fixture
def seed(sync_engine: sa.Engine) -> Iterator[BookingSeed]:
    data = seed_booking(sync_engine, conversations=3)
    yield data
    drop_tenant(sync_engine, data.tenant)


@pytest.fixture
def policy(config: TenantConfig) -> ChangePolicy:
    return ChangePolicy.from_rules(config.scheduling)


async def move(
    seed: BookingSeed,
    settings: Settings,
    policy: ChangePolicy,
    appointment: uuid.UUID,
    *,
    starts_at: datetime,
    provider: uuid.UUID | None = None,
    service: uuid.UUID | None = None,
    resource: uuid.UUID | None = None,
    contact: uuid.UUID | None = None,
    now: datetime = NOW,
) -> RescheduleResult:
    async with tenant_session(seed.tenant, settings) as session:
        return await booking.reschedule_appointment(
            session,
            tenant_id=seed.tenant,
            appointment_id=appointment,
            contact_id=contact or seed.contact,
            conversation_id=seed.conversations[0],
            provider_id=provider or seed.ana,
            service_id=service or seed.consulta,
            resource_id=resource,
            starts_at=starts_at,
            policy=policy,
            now=now,
        )


async def cancel(
    seed: BookingSeed,
    settings: Settings,
    policy: ChangePolicy,
    appointment: uuid.UUID,
    *,
    contact: uuid.UUID | None = None,
    now: datetime = NOW,
) -> CancelResult:
    async with tenant_session(seed.tenant, settings) as session:
        return await booking.cancel_appointment(
            session,
            tenant_id=seed.tenant,
            appointment_id=appointment,
            contact_id=contact or seed.contact,
            conversation_id=seed.conversations[0],
            policy=policy,
            reason=None,
            now=now,
        )


# ------------------------------------- a troca e atomica -------------------------------------


async def test_horario_vizinho_nao_conflita_com_o_proprio_agendamento(
    seed: BookingSeed, settings: Settings, policy: ChangePolicy, sync_engine: sa.Engine
) -> None:
    """08:00 -> 08:15 se sobrepoe ao 08:00 (30 min + buffer): o antigo sai antes do teste."""
    old = insert_appointment(sync_engine, seed, starts=EIGHT)

    result = await move(seed, settings, policy, old, starts_at=EIGHT_FIFTEEN)

    assert result.status == "rescheduled"
    assert result.appointment_id is not None
    assert appt(sync_engine, result.appointment_id).starts_at == EIGHT_FIFTEEN
    assert appt(sync_engine, old).status == "cancelled"
    assert active(sync_engine, seed) == 1


async def test_horario_ocupado_desfaz_a_troca_inteira(
    seed: BookingSeed, settings: Settings, policy: ChangePolicy, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=EIGHT)
    insert_appointment(sync_engine, seed, starts=NINE, contact=another_contact(sync_engine, seed))

    result = await move(seed, settings, policy, old, starts_at=NINE)

    assert result.status == "slot_taken"
    kept = appt(sync_engine, old)
    assert kept.status == "confirmed"
    assert kept.cancellation is None  # o savepoint desfez o cancelamento do antigo
    assert count(sync_engine, "appointments", seed.tenant) == 2


async def test_a_troca_libera_as_reservas_da_conversa_e_o_fracasso_as_devolve(
    seed: BookingSeed, settings: Settings, policy: ChangePolicy, sync_engine: sa.Engine
) -> None:
    """A reserva da propria conversa nao pode travar a remarcacao — mas so some se der certo."""
    old = insert_appointment(sync_engine, seed, starts=EIGHT)
    insert_appointment(sync_engine, seed, starts=NINE, contact=another_contact(sync_engine, seed))
    hold = rows(
        sync_engine,
        "INSERT INTO holds (tenant_id, conversation_id, provider_id, service_id, starts_at, "
        "ends_at, expires_at) VALUES (:t, :c, :p, :s, :a, :b, :e) RETURNING id",
        t=seed.tenant,
        c=seed.conversations[0],
        p=seed.ana,
        s=seed.consulta,
        a=combine_local(WED, time(10, 0), SP),
        b=combine_local(WED, time(10, 30), SP),
        e=NOW + timedelta(minutes=10),
    )[0][0]

    failed = await move(seed, settings, policy, old, starts_at=NINE)
    status_after_failure = rows(sync_engine, "SELECT status FROM holds WHERE id = :h", h=hold)[
        0
    ].status
    succeeded = await move(seed, settings, policy, old, starts_at=EIGHT_FIFTEEN)
    status_after_success = rows(sync_engine, "SELECT status FROM holds WHERE id = :h", h=hold)[
        0
    ].status

    assert failed.status == "slot_taken"
    assert status_after_failure == "active"
    assert succeeded.status == "rescheduled"
    assert status_after_success == "expired"


async def test_recurso_escasso_vale_tambem_para_quem_remarca(
    seed: BookingSeed, settings: Settings, policy: ChangePolicy, sync_engine: sa.Engine
) -> None:
    """O profissional esta livre, mas a sala (capacidade 1) esta ocupada."""
    ten = combine_local(WED, time(10, 0), SP)
    eleven = combine_local(WED, time(11, 0), SP)
    mine = insert_appointment(
        sync_engine, seed, starts=ten, service=seed.banho, resource=seed.room, minutes=60
    )
    insert_appointment(
        sync_engine,
        seed,
        starts=eleven,
        provider=seed.bruno,
        service=seed.banho,
        resource=seed.room,
        minutes=60,
        contact=another_contact(sync_engine, seed),
    )

    taken = await move(
        seed,
        settings,
        policy,
        mine,
        starts_at=eleven,
        service=seed.banho,
        resource=seed.room,
    )
    free = await move(
        seed,
        settings,
        policy,
        mine,
        starts_at=combine_local(WED, time(13, 30), SP),
        service=seed.banho,
        resource=seed.room,
    )

    assert taken.status == "slot_taken"
    assert free.status == "rescheduled"
    assert appt(sync_engine, free.appointment_id or uuid.uuid4()).status == "rescheduled"


async def test_recurso_que_o_servico_nao_usa_e_recusado(
    seed: BookingSeed, settings: Settings, policy: ChangePolicy, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=EIGHT)

    result = await move(seed, settings, policy, old, starts_at=NINE, resource=seed.room)

    assert result.status == "invalid"
    assert result.reason == "servico nao usa recurso"
    assert appt(sync_engine, old).status == "confirmed"


async def test_horario_no_passado_e_recusado(
    seed: BookingSeed, settings: Settings, policy: ChangePolicy, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)

    result = await move(seed, settings, policy, old, starts_at=NOW - timedelta(hours=1))

    assert result.status == "invalid"
    assert appt(sync_engine, old).status == "confirmed"


# --------------------------------------- reversao ---------------------------------------


async def test_reverter_reativa_o_antigo_com_o_status_que_ele_tinha(
    seed: BookingSeed, settings: Settings, policy: ChangePolicy, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR, status="rescheduled")
    moved = await move(seed, settings, policy, old, starts_at=EIGHT)
    assert moved.appointment_id is not None

    async with tenant_session(seed.tenant, settings) as session:
        restored = await booking.revert_reschedule(
            session,
            tenant_id=seed.tenant,
            appointment_id=moved.appointment_id,
            previous_id=old,
            conversation_id=seed.conversations[0],
            now=NOW,
        )

    assert restored is True
    assert appt(sync_engine, old).status == "rescheduled"  # nao vira `confirmed`
    assert appt(sync_engine, old).cancellation is None
    assert appt(sync_engine, moved.appointment_id).status == "cancelled"
    assert active(sync_engine, seed) == 1


async def test_reverter_duas_vezes_e_inofensivo(
    seed: BookingSeed, settings: Settings, policy: ChangePolicy, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)
    moved = await move(seed, settings, policy, old, starts_at=EIGHT)
    assert moved.appointment_id is not None

    answers = []
    for _ in range(2):
        async with tenant_session(seed.tenant, settings) as session:
            answers.append(
                await booking.revert_reschedule(
                    session,
                    tenant_id=seed.tenant,
                    appointment_id=moved.appointment_id,
                    previous_id=old,
                    conversation_id=seed.conversations[0],
                    now=NOW,
                )
            )

    assert answers == [True, True]
    assert active(sync_engine, seed) == 1


# ---------------------------------------- cancelamento ----------------------------------------


async def test_cancelar_um_agendamento_remarcado_nao_e_repeticao(
    seed: BookingSeed, settings: Settings, policy: ChangePolicy, sync_engine: sa.Engine
) -> None:
    """O antigo, cancelado por remarcacao, nao se cancela: o que vale e o novo."""
    old = insert_appointment(sync_engine, seed, starts=FAR)
    await move(seed, settings, policy, old, starts_at=EIGHT)

    result = await cancel(seed, settings, policy, old)

    assert result.status == "not_active"
    assert result.replayed is False


async def test_cancelar_duas_vezes_na_mesma_conversa_e_repeticao(
    seed: BookingSeed, settings: Settings, policy: ChangePolicy, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)

    first = await cancel(seed, settings, policy, old)
    second = await cancel(seed, settings, policy, old)

    assert (first.status, first.replayed) == ("cancelled", False)
    assert (second.status, second.replayed) == ("cancelled", True)
    assert active(sync_engine, seed) == 0


# ---------------------------------------- a politica ----------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("reschedule", "too_late"), ("cancel", "too_late")],
)
async def test_check_change_abaixo_do_minimo(
    kind: ChangeKind,
    expected: str,
    seed: BookingSeed,
    settings: Settings,
    policy: ChangePolicy,
    sync_engine: sa.Engine,
) -> None:
    near = insert_appointment(sync_engine, seed, starts=NEAR)
    edge = insert_appointment(sync_engine, seed, starts=EDGE, provider=seed.bruno)
    cancelled = insert_appointment(sync_engine, seed, starts=FAR, status="cancelled")
    verdicts = {}
    async with tenant_session(seed.tenant, settings) as session:
        for name, appointment in {
            "near": near,
            "edge": edge,
            "cancelled": cancelled,
            "missing": uuid.uuid4(),
        }.items():
            verdicts[name] = await booking.check_change(
                session,
                tenant_id=seed.tenant,
                appointment_id=appointment,
                contact_id=seed.contact,
                kind=kind,
                policy=policy,
                now=NOW,
            )

    assert verdicts == {
        "near": expected,
        "edge": "ok",
        "cancelled": "not_active",
        "missing": "not_found",
    }


# --------------------------------------- concorrencia ---------------------------------------


async def test_seis_clientes_disputam_o_mesmo_horario_e_so_um_leva(
    seed: BookingSeed, settings: Settings, policy: ChangePolicy, sync_engine: sa.Engine
) -> None:
    """Cada um tem hora com o Bruno e quer as 8h da Ana; quem perde fica como estava."""
    contacts = [another_contact(sync_engine, seed) for _ in range(6)]
    olds = [
        insert_appointment(
            sync_engine,
            seed,
            starts=combine_local(WED, time(14 + n // 2, 30 * (n % 2)), SP),
            provider=seed.bruno,
            contact=contact,
        )
        for n, contact in enumerate(contacts)
    ]

    results = await asyncio.gather(
        *(
            move(seed, settings, policy, old, starts_at=EIGHT, contact=contact)
            for old, contact in zip(olds, contacts, strict=True)
        )
    )

    assert sorted(result.status for result in results) == ["rescheduled"] + ["slot_taken"] * 5
    winner = next(result for result in results if result.status == "rescheduled")
    assert winner.appointment_id is not None
    # Cinco ficaram com o agendamento original; um tem so o novo. Nenhum ficou sem nada.
    assert active(sync_engine, seed) == 6
    assert appt(sync_engine, winner.appointment_id).starts_at == EIGHT
    losers = [
        old for old, result in zip(olds, results, strict=True) if result.status != "rescheduled"
    ]
    assert all(appt(sync_engine, old).status == "confirmed" for old in losers)


async def test_o_mesmo_pedido_repetido_em_paralelo_cria_um_agendamento_so(
    seed: BookingSeed, settings: Settings, policy: ChangePolicy, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)

    results = await asyncio.gather(
        *(move(seed, settings, policy, old, starts_at=EIGHT) for _ in range(5))
    )

    assert {result.status for result in results} == {"rescheduled"}
    assert len({result.appointment_id for result in results}) == 1
    assert sum(not result.replayed for result in results) == 1
    assert count(sync_engine, "appointments", seed.tenant) == 2


async def test_remarcar_e_cancelar_ao_mesmo_tempo_deixam_um_estado_so(
    seed: BookingSeed, settings: Settings, policy: ChangePolicy, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)

    moved, cancelled = await asyncio.gather(
        move(seed, settings, policy, old, starts_at=EIGHT),
        cancel(seed, settings, policy, old),
    )

    # Ou o cancelamento ganhou (e a remarcacao nao acha o que mover), ou o contrario.
    if moved.status == "rescheduled":
        assert cancelled.status == "not_active"
        assert active(sync_engine, seed) == 1
    else:
        assert (moved.status, cancelled.status) == ("not_active", "cancelled")
        assert active(sync_engine, seed) == 0
