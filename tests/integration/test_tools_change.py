"""Remarcar e cancelar contra Postgres real (secoes 12.2 e 12.3; aceite da Sessao S16).

Tres blocos, na ordem do aceite:

1. **Antecedencia.** Dentro do prazo a mudanca acontece; abaixo de `*_min_notice_hours` (ou com
   `allow_*: false`) nada e gravado e a conversa escala por regra, sem o modelo pedir.
2. **Remarcacao transacional.** Cria o novo, cancela o antigo e move o evento; quando qualquer
   passo falha, volta tudo e escala.
3. **Cancelamento em dois turnos.** A primeira chamada pergunta e nao cancela; o token so
   serve a um turno posterior.

O ultimo bloco roda o motor inteiro, com o modelo tentando o atalho.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from app.agent.audit import NullAuditSink
from app.agent.engine import AgentEngine, ConversationState, TurnRequest
from app.agent.guardrails import OutputGuardrail
from app.agent.llm import LLMMessage, LLMResponse, PromptSegment, ToolResultBlock, ToolSchema
from app.agent.memory import StoredMessage
from app.agent.tools.base import ToolContext, ToolSpec
from app.agent.tools.cancel_appointment import (
    SlotReleased,
    cancel_appointment_tool,
)
from app.agent.tools.check_availability import check_availability_tool
from app.agent.tools.common import slot_signer
from app.agent.tools.find_appointments import find_appointments_tool
from app.agent.tools.registry import build_registry
from app.agent.tools.reschedule_appointment import reschedule_appointment_tool
from app.calendar.base import (
    CalendarSyncPending,
)
from app.calendar.resolver import no_calendars
from app.core.config import Settings
from app.core.db import dispose_engine
from app.domain.tenant_config import TenantConfig, load_tenant_config
from app.scheduling.availability import CalendarResolver
from app.scheduling.slot_token import SlotClaims
from tests.conftest import TENANTS_DIR
from tests.fakes import ScriptedProvider, text_response, tool_response
from tests.integration.booking_seed import BookingSeed, drop_tenant, seed_booking
from tests.integration.change_support import (
    ANSWERED,
    EDGE,
    FAR,
    NEAR,
    NOW,
    WED,
    FakeCalendar,
    active,
    another_contact,
    appt,
    count,
    ctx_for,
    give_calendar,
    insert_appointment,
    iso_instants,
    resolver_of,
    rows,
    with_scheduling,
)

pytestmark = pytest.mark.integration


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


@dataclass
class Tools:
    check: ToolSpec
    find: ToolSpec
    reschedule: ToolSpec
    cancel: ToolSpec


def tools_for(
    settings: Settings,
    calendars: CalendarResolver = no_calendars,
    on_slot_released: Callable[[SlotReleased], Any] | None = None,
) -> Tools:
    return Tools(
        check=check_availability_tool(calendars, settings=settings),
        find=find_appointments_tool(settings=settings),
        reschedule=reschedule_appointment_tool(calendars, settings=settings),
        cancel=cancel_appointment_tool(
            calendars, settings=settings, on_slot_released=on_slot_released
        ),
    )


async def offer(
    tools: Tools, ctx: ToolContext, service: uuid.UUID, *, day: date = WED, **extra: Any
) -> dict[str, Any]:
    window = {"date_from": day.isoformat(), "date_to": day.isoformat()}
    result = await tools.check.handler(ctx, {"service_id": str(service), **window, **extra})
    assert not result.is_error, result.content
    return dict(result.content)


async def slot_for(
    tools: Tools, ctx: ToolContext, seed: BookingSeed, provider: uuid.UUID | None = None
) -> dict[str, Any]:
    """O primeiro horario de quarta que `check_availability` oferece para o profissional."""
    content = await offer(tools, ctx, seed.consulta, provider_id=str(provider or seed.ana), limit=1)
    slot: dict[str, Any] = content["slots"][0]
    return slot


async def reschedule(
    tools: Tools, ctx: ToolContext, appointment: uuid.UUID, slot: Mapping[str, Any]
) -> Any:
    return await tools.reschedule.handler(
        ctx, {"appointment_id": str(appointment), "slot_token": slot["slot_token"]}
    )


# ==================================== find_appointments ====================================


async def test_find_diz_o_que_a_politica_deixa_fazer_por_aqui(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    far = insert_appointment(sync_engine, seed, starts=FAR)
    near = insert_appointment(sync_engine, seed, starts=NEAR, provider=seed.bruno)
    edge = insert_appointment(sync_engine, seed, starts=EDGE, provider=seed.bruno)

    result = await tools_for(settings).find.handler(ctx_for(seed, config), {})

    by_id = {item["appointment_id"]: item for item in result.content["appointments"]}
    assert (by_id[str(near)]["can_reschedule"], by_id[str(near)]["can_cancel"]) == (False, False)
    assert (by_id[str(edge)]["can_reschedule"], by_id[str(edge)]["can_cancel"]) == (True, True)
    assert (by_id[str(far)]["can_reschedule"], by_id[str(far)]["can_cancel"]) == (True, True)
    assert by_id[str(far)]["service_id"] == str(seed.consulta)


async def test_find_respeita_o_que_o_tenant_desligou(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    insert_appointment(sync_engine, seed, starts=FAR)
    off = with_scheduling(config, allow_reschedule=False)

    result = await tools_for(settings).find.handler(ctx_for(seed, off), {})

    item = result.content["appointments"][0]
    assert (item["can_reschedule"], item["can_cancel"]) == (False, True)


# ======================================== remarcacao ========================================


async def test_remarca_cria_o_novo_cancela_o_antigo_e_libera_o_horario(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    slot = await slot_for(tools, ctx, seed)

    result = await reschedule(tools, ctx, old, slot)

    assert not result.is_error, result.content
    content = result.content
    assert content["status"] == "rescheduled"
    assert content["previous_appointment_id"] == str(old)
    assert content["starts_at"] == slot["starts_at"]
    assert content["human"] == slot["human"]
    assert content["provider_name"] == "Ana"
    assert content["service"] == "Consulta"
    assert content["subject_name"] == "Mia"
    # So o inicio do atendimento prova horario (D-55): o horario antigo nao sai no resultado.
    assert iso_instants(content) == [slot["starts_at"]]
    assert "ends_at" not in json.dumps(content)

    new = appt(sync_engine, content["appointment_id"])
    assert new.status == "rescheduled"
    assert new.starts_at == datetime.fromisoformat(slot["starts_at"])
    assert new.sync_status == "synced"  # agenda propria: nada a espelhar
    assert new.conversation_id == seed.conversations[0]
    assert new.intake["subject_name"] == "Mia"
    assert new.intake["_rescheduled_from"] == str(old)

    previous = appt(sync_engine, old)
    assert previous.status == "cancelled"
    assert previous.cancellation["kind"] == "reschedule"
    assert previous.cancellation["replaced_by"] == content["appointment_id"]
    assert previous.cancellation["previous_status"] == "confirmed"
    assert previous.cancellation["conversation_id"] == str(seed.conversations[0])
    assert active(sync_engine, seed) == 1

    # O horario antigo voltou para a oferta.
    freed = await offer(
        tools, ctx, seed.consulta, provider_id=str(seed.ana), preferred_periods=["afternoon"],
        limit=10,
    )  # fmt: skip
    assert FAR.isoformat() in {
        datetime.fromisoformat(s["starts_at"]).isoformat() for s in freed["slots"]
    }


async def test_o_novo_agendamento_remarcado_aparece_na_listagem(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    slot = await slot_for(tools, ctx, seed)
    await reschedule(tools, ctx, old, slot)

    listed = await tools.find.handler(ctx, {})

    items = listed.content["appointments"]
    assert [item["starts_at"] for item in items] == [slot["starts_at"]]
    assert items[0]["status"] == "rescheduled"


async def test_remarcar_exatamente_no_minimo_ainda_passa(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=EDGE)
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)

    result = await reschedule(tools, ctx, old, await slot_for(tools, ctx, seed))

    assert result.content["status"] == "rescheduled"


async def test_remarcar_abaixo_do_minimo_escala_e_nada_muda(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """Aceite da S16: fora do prazo, a tool devolve `needs_human` e o banco fica intacto."""
    old = insert_appointment(sync_engine, seed, starts=NEAR)
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    slot = await slot_for(tools, ctx, seed)

    result = await reschedule(tools, ctx, old, slot)

    assert result.is_error
    assert result.content["status"] == "needs_human"
    assert result.content["reason"] == "remarcacao_fora_do_prazo"
    assert result.content["suggested_action"]
    assert appt(sync_engine, old).status == "confirmed"
    assert count(sync_engine, "appointments", seed.tenant) == 1


async def test_o_numero_de_horas_nao_vai_no_texto_para_o_modelo(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """ "4 horas" na boca do modelo seria lido pelo guardrail de saida como um horario."""
    old = insert_appointment(sync_engine, seed, starts=NEAR)

    result = await tools_for(settings).reschedule.handler(
        ctx_for(seed, config), {"appointment_id": str(old)}
    )

    assert not re.search(r"\d", json.dumps(result.content["message"]))


async def test_sem_horario_a_tool_so_decide_a_politica(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """`can_reschedule: false`: o modelo nao tem token e nao precisa inventar um."""
    near = insert_appointment(sync_engine, seed, starts=NEAR)
    far = insert_appointment(sync_engine, seed, starts=FAR, provider=seed.bruno)
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)

    blocked = await tools.reschedule.handler(ctx, {"appointment_id": str(near)})
    allowed = await tools.reschedule.handler(ctx, {"appointment_id": str(far)})

    assert blocked.content["status"] == "needs_human"
    assert blocked.content["reason"] == "remarcacao_fora_do_prazo"
    assert allowed.is_error
    assert allowed.content["status"] == "slot_token_required"
    assert allowed.content["suggested_action"] == "call check_availability again"


async def test_remarcacao_desligada_no_tenant_vai_para_a_equipe(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)
    off = with_scheduling(config, allow_reschedule=False)
    tools = tools_for(settings)
    slot = await slot_for(tools, ctx_for(seed, off), seed)

    result = await reschedule(tools, ctx_for(seed, off), old, slot)

    assert result.content["status"] == "needs_human"
    assert result.content["reason"] == "remarcacao_nao_permitida"
    assert appt(sync_engine, old).status == "confirmed"


async def test_o_minimo_vem_da_config_do_tenant(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """Nada de "4" no codigo: o mesmo agendamento passa ou escala conforme o tenant."""
    old = insert_appointment(sync_engine, seed, starts=NOW + timedelta(hours=10))
    strict = with_scheduling(config, reschedule_min_notice_hours=24)
    tools = tools_for(settings)

    refused = await reschedule(
        tools, ctx_for(seed, strict), old, await slot_for(tools, ctx_for(seed, strict), seed)
    )
    accepted = await reschedule(
        tools, ctx_for(seed, config), old, await slot_for(tools, ctx_for(seed, config), seed)
    )

    assert refused.content["status"] == "needs_human"
    assert accepted.content["status"] == "rescheduled"


async def test_agendamento_de_outro_contato_nao_se_remarca(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    theirs = insert_appointment(
        sync_engine, seed, starts=FAR, contact=another_contact(sync_engine, seed)
    )
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)

    result = await reschedule(tools, ctx, theirs, await slot_for(tools, ctx, seed))

    assert result.is_error
    assert result.content["status"] == "appointment_not_found"
    assert appt(sync_engine, theirs).status == "confirmed"
    assert count(sync_engine, "appointments", seed.tenant) == 1


async def test_agendamento_ja_cancelado_nao_se_remarca(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR, status="cancelled")
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)

    result = await reschedule(tools, ctx, old, await slot_for(tools, ctx, seed))

    assert result.content["status"] == "appointment_not_active"
    assert "find_appointments" in result.content["suggested_action"]


async def test_horario_ocupado_no_meio_do_caminho_e_slot_taken_e_o_antigo_segue(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    slot = await slot_for(tools, ctx, seed)
    # Outra pessoa agenda o mesmo horario depois da oferta.
    insert_appointment(
        sync_engine,
        seed,
        starts=datetime.fromisoformat(slot["starts_at"]),
        contact=another_contact(sync_engine, seed),
    )

    result = await reschedule(tools, ctx, old, slot)

    assert result.is_error
    assert result.content["status"] == "slot_taken"
    assert result.content["suggested_action"] == "call check_availability again"
    assert appt(sync_engine, old).status == "confirmed"
    assert appt(sync_engine, old).cancellation is None
    assert active(sync_engine, seed) == 2  # o antigo e o do outro; nada novo


async def test_remarcar_nao_troca_de_servico(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)  # consulta
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    bath = (await offer(tools, ctx, seed.banho, limit=1))["slots"][0]

    result = await reschedule(tools, ctx, old, bath)

    assert result.content["status"] == "wrong_service"
    assert appt(sync_engine, old).status == "confirmed"


async def test_remarcar_para_o_mesmo_horario_e_recusado(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)
    same = slot_signer(settings).sign(
        SlotClaims(provider_id=seed.ana, service_id=seed.consulta, starts_at=FAR),
        tenant_id=seed.tenant,
        conversation_id=seed.conversations[0],
        now=NOW,
    )

    result = await reschedule(tools_for(settings), ctx_for(seed, config), old, {"slot_token": same})

    assert result.content["status"] == "same_slot"
    assert appt(sync_engine, old).status == "confirmed"


async def test_horario_que_nao_veio_de_check_availability_nao_remarca(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    token = (await slot_for(tools, ctx, seed))["slot_token"]
    payload, signature = token.split(".")
    flipped = "A" if payload[5] != "A" else "B"
    tampered = f"{payload[:5]}{flipped}{payload[6:]}.{signature}"

    for forged in (tampered, "eyJ2IjoxfQ.assinatura-inventada"):
        result = await reschedule(tools, ctx, old, {"slot_token": forged})
        assert result.is_error
        assert result.content["status"] == "invalid_slot_token"
    # E o token de outra conversa tambem nao vale aqui.
    stolen = await reschedule(
        tools, ctx_for(seed, config, conversation=1), old, {"slot_token": token}
    )
    assert stolen.content["status"] == "invalid_slot_token"
    assert appt(sync_engine, old).status == "confirmed"
    assert count(sync_engine, "appointments", seed.tenant) == 1


async def test_repetir_a_remarcacao_devolve_o_mesmo_agendamento(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    give_calendar(sync_engine, seed.ana, "ana@cal")
    old = insert_appointment(sync_engine, seed, starts=FAR, event_id="evt-old")
    calendar = FakeCalendar()
    calendar.put("ana@cal", "evt-old", FAR)
    tools = tools_for(settings, resolver_of(calendar))
    ctx = ctx_for(seed, config)
    slot = await slot_for(tools, ctx, seed)

    first = await reschedule(tools, ctx, old, slot)
    second = await reschedule(tools, ctx_for(seed, config, now=ANSWERED), old, slot)

    assert second.content["status"] == "rescheduled"
    assert second.content["appointment_id"] == first.content["appointment_id"]
    assert count(sync_engine, "appointments", seed.tenant) == 2  # o antigo e o novo, so
    # A segunda vez nao mexe no calendario: o evento ja foi movido na primeira.
    assert calendar.calls == [("update", "ana@cal", "evt-old")]


async def test_remarcar_de_novo_um_agendamento_ja_remarcado(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    first = await reschedule(tools, ctx, old, await slot_for(tools, ctx, seed))
    second_slot = (await offer(tools, ctx, seed.consulta, provider_id=str(seed.bruno), limit=1))[
        "slots"
    ][0]

    second = await reschedule(tools, ctx, uuid.UUID(first.content["appointment_id"]), second_slot)

    assert second.content["status"] == "rescheduled"
    assert second.content["provider_name"] == "Bruno"
    assert active(sync_engine, seed) == 1
    assert appt(sync_engine, first.content["appointment_id"]).status == "cancelled"
    # Desfazer lembra que o anterior era `rescheduled`, nao `confirmed`.
    previous = appt(sync_engine, first.content["appointment_id"]).cancellation
    assert previous["previous_status"] == "rescheduled"


async def test_falha_inesperada_desfaz_tudo_e_escala(
    seed: BookingSeed,
    config: TenantConfig,
    settings: Settings,
    sync_engine: sa.Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Se qualquer passo falhar, rollback e escalonamento" (12.2): o erro vem depois de o
    antigo ja ter sido cancelado dentro da transacao, e nada disso pode sobrar."""
    from app.scheduling import booking

    old = insert_appointment(sync_engine, seed, starts=FAR)
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    slot = await slot_for(tools, ctx, seed)

    async def broken(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("banco caiu no meio da remarcacao")

    monkeypatch.setattr(booking, "_conflicts", broken)
    result = await reschedule(tools, ctx, old, slot)

    assert result.is_error
    assert result.content["status"] == "needs_human"
    assert result.content["reason"] == "falha_ao_remarcar"
    assert result.content["original_kept"] is True
    assert "banco caiu" not in json.dumps(result.content)
    previous = appt(sync_engine, old)
    assert previous.status == "confirmed"
    assert previous.cancellation is None
    assert count(sync_engine, "appointments", seed.tenant) == 1


# -------------------------------- remarcacao: o calendario --------------------------------


async def test_mesmo_calendario_move_o_mesmo_evento(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    give_calendar(sync_engine, seed.ana, "ana@cal")
    old = insert_appointment(sync_engine, seed, starts=FAR, event_id="evt-old")
    calendar = FakeCalendar()
    calendar.put("ana@cal", "evt-old", FAR)
    tools = tools_for(settings, resolver_of(calendar))
    ctx = ctx_for(seed, config)
    slot = await slot_for(tools, ctx, seed)

    result = await reschedule(tools, ctx, old, slot)

    assert result.content["status"] == "rescheduled"
    assert calendar.calls == [("update", "ana@cal", "evt-old")]
    moved = calendar.events[("ana@cal", "evt-old")]
    assert moved.start == datetime.fromisoformat(slot["starts_at"])
    assert moved.end - moved.start == timedelta(minutes=30)
    # O evento e do agendamento novo agora: so um agendamento aponta para ele.
    new = appt(sync_engine, result.content["appointment_id"])
    assert (new.external_event_id, new.sync_status) == ("evt-old", "synced")
    assert appt(sync_engine, old).external_event_id is None


async def test_outro_profissional_cria_o_evento_novo_e_apaga_o_antigo(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    give_calendar(sync_engine, seed.ana, "ana@cal")
    give_calendar(sync_engine, seed.bruno, "bruno@cal")
    old = insert_appointment(sync_engine, seed, starts=FAR, event_id="evt-old")
    calendar = FakeCalendar()
    calendar.put("ana@cal", "evt-old", FAR)
    tools = tools_for(settings, resolver_of(calendar))
    ctx = ctx_for(seed, config)

    result = await reschedule(tools, ctx, old, await slot_for(tools, ctx, seed, seed.bruno))

    new_id = result.content["appointment_id"]
    assert [call[:2] for call in calendar.calls] == [("create", "bruno@cal"), ("delete", "ana@cal")]
    assert calendar.calls[0][2] == new_id  # iCalUID = appointment_id (13.4)
    assert set(calendar.events) == {("bruno@cal", f"evt-{new_id[:8]}")}
    draft = calendar.events[("bruno@cal", f"evt-{new_id[:8]}")]
    assert draft.summary == "Consulta - Mia"
    assert "Contato: Carla" in (draft.description or "")
    new = appt(sync_engine, new_id)
    assert (new.external_event_id, new.sync_status) == (f"evt-{new_id[:8]}", "synced")
    assert appt(sync_engine, old).external_event_id is None


async def test_agenda_propria_nao_toca_o_calendario(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)
    calendar = FakeCalendar()
    tools = tools_for(settings, resolver_of(calendar))
    ctx = ctx_for(seed, config)

    result = await reschedule(tools, ctx, old, await slot_for(tools, ctx, seed))

    assert result.content["status"] == "rescheduled"
    assert calendar.calls == []


async def test_evento_que_nao_anda_desfaz_a_remarcacao_e_escala(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """O profissional esperaria o cliente na hora errada: melhor manter o horario antigo."""
    give_calendar(sync_engine, seed.ana, "ana@cal")
    old = insert_appointment(sync_engine, seed, starts=FAR, event_id="evt-old")
    calendar = FakeCalendar()
    calendar.put("ana@cal", "evt-old", FAR)
    calendar.update_error = CalendarSyncPending("google 503", attempts=5)
    tools = tools_for(settings, resolver_of(calendar))
    ctx = ctx_for(seed, config)

    result = await reschedule(tools, ctx, old, await slot_for(tools, ctx, seed))

    assert result.is_error
    assert result.content["status"] == "needs_human"
    assert result.content["reason"] == "falha_ao_remarcar"
    assert result.content["original_kept"] is True
    assert "503" not in json.dumps(result.content)
    restored = appt(sync_engine, old)
    assert restored.status == "confirmed"
    assert restored.cancellation is None
    assert restored.external_event_id == "evt-old"
    assert calendar.events[("ana@cal", "evt-old")].start == FAR
    assert active(sync_engine, seed) == 1
    failed = rows(
        sync_engine,
        "SELECT status, cancellation FROM appointments WHERE tenant_id = :t AND id <> :o",
        t=seed.tenant,
        o=old,
    )
    assert [(row.status, row.cancellation["kind"]) for row in failed] == [
        ("cancelled", "reschedule_failed")
    ]


async def test_excecao_inesperada_do_calendario_tambem_desfaz(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    give_calendar(sync_engine, seed.ana, "ana@cal")
    old = insert_appointment(sync_engine, seed, starts=FAR, event_id="evt-old")
    calendar = FakeCalendar()
    calendar.put("ana@cal", "evt-old", FAR)
    calendar.update_error = RuntimeError("bug no cliente do calendario")
    tools = tools_for(settings, resolver_of(calendar))
    ctx = ctx_for(seed, config)

    result = await reschedule(tools, ctx, old, await slot_for(tools, ctx, seed))

    assert result.content["status"] == "needs_human"
    assert appt(sync_engine, old).status == "confirmed"


async def test_horario_antigo_ocupado_na_reversao_nao_e_reativado(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """Reativar o antigo em cima de quem acabou de pegar o horario seria overbooking (6)."""
    give_calendar(sync_engine, seed.ana, "ana@cal")
    old = insert_appointment(sync_engine, seed, starts=FAR, event_id="evt-old")
    stranger = another_contact(sync_engine, seed)
    calendar = FakeCalendar()
    calendar.put("ana@cal", "evt-old", FAR)
    calendar.update_error = CalendarSyncPending("google 503", attempts=5)
    calendar.during_update = lambda: insert_appointment(
        sync_engine, seed, starts=FAR, contact=stranger
    )
    tools = tools_for(settings, resolver_of(calendar))
    ctx = ctx_for(seed, config)

    result = await reschedule(tools, ctx, old, await slot_for(tools, ctx, seed))

    assert result.content["status"] == "needs_human"
    assert result.content["original_kept"] is False
    assert "continua valendo" not in result.content["message"]
    assert appt(sync_engine, old).status == "cancelled"
    assert active(sync_engine, seed) == 1  # so o de quem pegou o horario


async def test_troca_de_profissional_que_nao_apaga_o_antigo_desfaz_o_evento_novo(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    give_calendar(sync_engine, seed.ana, "ana@cal")
    give_calendar(sync_engine, seed.bruno, "bruno@cal")
    old = insert_appointment(sync_engine, seed, starts=FAR, event_id="evt-old")
    calendar = FakeCalendar()
    calendar.put("ana@cal", "evt-old", FAR)
    calendar.delete_errors["ana@cal"] = CalendarSyncPending("google 503", attempts=5)
    tools = tools_for(settings, resolver_of(calendar))
    ctx = ctx_for(seed, config)

    result = await reschedule(tools, ctx, old, await slot_for(tools, ctx, seed, seed.bruno))

    assert result.content["status"] == "needs_human"
    assert result.content["original_kept"] is True
    # Nao sobra evento orfao na agenda do Bruno, e o da Ana continua onde estava.
    assert set(calendar.events) == {("ana@cal", "evt-old")}
    assert appt(sync_engine, old).status == "confirmed"
    assert appt(sync_engine, old).external_event_id == "evt-old"


async def test_sem_evento_anterior_calendario_fora_do_ar_nao_desfaz(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """Como na confirmacao (D-57): sem evento antigo para trair, o banco manda."""
    give_calendar(sync_engine, seed.ana, "ana@cal")
    old = insert_appointment(sync_engine, seed, starts=FAR, sync_status="pending_sync")
    calendar = FakeCalendar()
    calendar.create_error = CalendarSyncPending("google 503", attempts=5)
    tools = tools_for(settings, resolver_of(calendar))
    ctx = ctx_for(seed, config)

    result = await reschedule(tools, ctx, old, await slot_for(tools, ctx, seed))

    assert result.content["status"] == "rescheduled"
    assert appt(sync_engine, result.content["appointment_id"]).sync_status == "pending_sync"
    assert appt(sync_engine, old).status == "cancelled"


async def test_evento_existente_sem_credencial_desfaz(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    give_calendar(sync_engine, seed.ana, "ana@cal")
    old = insert_appointment(sync_engine, seed, starts=FAR, event_id="evt-old")
    offering = tools_for(settings, resolver_of(FakeCalendar()))
    moving = tools_for(settings, no_calendars)  # o resolver perdeu a credencial
    ctx = ctx_for(seed, config)
    slot = await slot_for(offering, ctx, seed)

    result = await reschedule(moving, ctx, old, slot)

    assert result.content["status"] == "needs_human"
    assert appt(sync_engine, old).status == "confirmed"


# ========================================= cancelamento =========================================


async def ask(tools: Tools, ctx: ToolContext, appointment: uuid.UUID) -> Any:
    return await tools.cancel.handler(ctx, {"appointment_id": str(appointment)})


async def confirm(
    tools: Tools, ctx: ToolContext, appointment: uuid.UUID, token: str, **extra: Any
) -> Any:
    return await tools.cancel.handler(
        ctx, {"appointment_id": str(appointment), "confirmation_token": token, **extra}
    )


async def test_a_primeira_chamada_pergunta_e_nao_cancela_nada(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)

    result = await ask(tools_for(settings), ctx_for(seed, config), old)

    assert not result.is_error
    content = result.content
    assert content["status"] == "confirmation_required"
    assert content["confirmation_token"]
    assert content["appointment_id"] == str(old)
    assert content["human"] == "quarta-feira, 11 de novembro, às 14h"
    assert content["service"] == "Consulta"
    assert content["subject_name"] == "Mia"
    assert "Nada foi cancelado" in content["message"]
    assert iso_instants(content) == [content["starts_at"]]
    row = appt(sync_engine, old)
    assert row.status == "confirmed"
    assert row.cancellation is None


async def test_token_no_mesmo_turno_nao_cancela(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """Aceite da S16: o modelo nao cancela em uma mensagem so, mesmo querendo."""
    old = insert_appointment(sync_engine, seed, starts=FAR)
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    token = (await ask(tools, ctx, old)).content["confirmation_token"]

    result = await confirm(tools, ctx, old, token)

    assert result.is_error
    assert result.content["status"] == "confirmation_pending"
    assert appt(sync_engine, old).status == "confirmed"


async def test_confirmando_no_turno_seguinte_cancela(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)
    tools = tools_for(settings)
    token = (await ask(tools, ctx_for(seed, config), old)).content["confirmation_token"]

    result = await confirm(
        tools, ctx_for(seed, config, now=ANSWERED), old, token, reason="vou viajar"
    )

    assert not result.is_error, result.content
    assert result.content["status"] == "cancelled"
    assert result.content["appointment_id"] == str(old)
    assert result.content["human"] == "quarta-feira, 11 de novembro, às 14h"
    assert "remarcar" in result.content["message"]
    row = appt(sync_engine, old)
    assert row.status == "cancelled"
    assert row.cancellation["kind"] == "cancel"
    assert row.cancellation["reason"] == "vou viajar"
    assert row.cancellation["previous_status"] == "confirmed"
    assert row.cancellation["conversation_id"] == str(seed.conversations[0])
    assert active(sync_engine, seed) == 0


async def test_cancelamento_sem_motivo_grava_sem_motivo(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)
    tools = tools_for(settings)
    token = (await ask(tools, ctx_for(seed, config), old)).content["confirmation_token"]

    await confirm(tools, ctx_for(seed, config, now=ANSWERED), old, token, reason="   ")

    assert "reason" not in appt(sync_engine, old).cancellation


async def test_documento_no_motivo_e_redigido_e_o_cancelamento_segue(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """O motivo e texto livre que vai para o painel: CPF e cartao nao ficam gravados (18.2)."""
    old = insert_appointment(sync_engine, seed, starts=FAR)
    tools = tools_for(settings)
    token = (await ask(tools, ctx_for(seed, config), old)).content["confirmation_token"]

    result = await confirm(
        tools,
        ctx_for(seed, config, now=ANSWERED),
        old,
        token,
        reason="troquei de clinica, meu cpf 123.456.789-09",
    )

    assert result.content["status"] == "cancelled"
    stored = appt(sync_engine, old).cancellation["reason"]
    assert "123.456.789-09" not in stored
    assert stored.startswith("troquei de clinica")


async def test_o_horario_cancelado_volta_para_a_oferta(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)
    tools = tools_for(settings)
    token = (await ask(tools, ctx_for(seed, config), old)).content["confirmation_token"]
    await confirm(tools, ctx_for(seed, config, now=ANSWERED), old, token)

    freed = await offer(
        tools,
        ctx_for(seed, config, now=ANSWERED),
        seed.consulta,
        provider_id=str(seed.ana),
        preferred_periods=["afternoon"],
        limit=10,
    )

    starts = {datetime.fromisoformat(s["starts_at"]).isoformat() for s in freed["slots"]}
    assert FAR.isoformat() in starts


async def test_token_de_outro_agendamento_de_outra_conversa_ou_vencido_nao_vale(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    mine = insert_appointment(sync_engine, seed, starts=FAR)
    other = insert_appointment(sync_engine, seed, starts=FAR + timedelta(days=1))
    tools = tools_for(settings)
    token = (await ask(tools, ctx_for(seed, config), mine)).content["confirmation_token"]
    ttl = timedelta(minutes=settings.confirmation_token_ttl_minutes)

    attempts = {
        "outro agendamento": (ctx_for(seed, config, now=ANSWERED), other, token),
        "outra conversa": (ctx_for(seed, config, conversation=1, now=ANSWERED), mine, token),
        "vencido": (ctx_for(seed, config, now=NOW + ttl + timedelta(seconds=1)), mine, token),
        "adulterado": (ctx_for(seed, config, now=ANSWERED), mine, token[:-3] + "AAA"),
        "inventado": (ctx_for(seed, config, now=ANSWERED), mine, "eyJ2IjoxfQ.inventado"),
    }
    for name, (ctx, appointment, presented) in attempts.items():
        result = await confirm(tools, ctx, appointment, presented)
        assert result.is_error, name
        assert result.content["status"] == "invalid_confirmation_token", name
        assert "only the appointment_id" in result.content["suggested_action"], name

    assert appt(sync_engine, mine).status == "confirmed"
    assert appt(sync_engine, other).status == "confirmed"


async def test_cancelar_abaixo_do_minimo_escala_ja_na_pergunta(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """Nao se pede confirmacao de algo que nao sera feito."""
    old = insert_appointment(sync_engine, seed, starts=NEAR)

    result = await ask(tools_for(settings), ctx_for(seed, config), old)

    assert result.is_error
    assert result.content["status"] == "needs_human"
    assert result.content["reason"] == "cancelamento_fora_do_prazo"
    assert "confirmation_token" not in result.content
    assert appt(sync_engine, old).status == "confirmed"


async def test_o_prazo_fecha_entre_a_pergunta_e_a_resposta(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """Perguntou a 4h10 do atendimento, o cliente respondeu a 3h50: ja e a equipe."""
    old = insert_appointment(sync_engine, seed, starts=EDGE + timedelta(minutes=10))
    tools = tools_for(settings)
    token = (await ask(tools, ctx_for(seed, config), old)).content["confirmation_token"]

    result = await confirm(
        tools, ctx_for(seed, config, now=NOW + timedelta(minutes=20)), old, token
    )

    assert result.content["status"] == "needs_human"
    assert result.content["reason"] == "cancelamento_fora_do_prazo"
    assert appt(sync_engine, old).status == "confirmed"


async def test_cancelar_exatamente_no_minimo_ainda_passa(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=EDGE + timedelta(minutes=3))
    tools = tools_for(settings)
    token = (await ask(tools, ctx_for(seed, config), old)).content["confirmation_token"]

    result = await confirm(tools, ctx_for(seed, config, now=ANSWERED), old, token)

    assert result.content["status"] == "cancelled"


async def test_cancelamento_desligado_no_tenant_vai_para_a_equipe(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)
    off = with_scheduling(config, allow_cancel=False)

    result = await ask(tools_for(settings), ctx_for(seed, off), old)

    assert result.content["status"] == "needs_human"
    assert result.content["reason"] == "cancelamento_nao_permitido"


async def test_agendamento_de_outro_contato_nao_se_cancela(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    theirs = insert_appointment(
        sync_engine, seed, starts=FAR, contact=another_contact(sync_engine, seed)
    )

    result = await ask(tools_for(settings), ctx_for(seed, config), theirs)

    assert result.is_error
    assert result.content["status"] == "appointment_not_found"
    assert "confirmation_token" not in result.content


async def test_agendamento_inexistente_ou_ja_cancelado(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    cancelled = insert_appointment(sync_engine, seed, starts=FAR, status="cancelled")
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)

    missing = await ask(tools, ctx, uuid.uuid4())
    done = await ask(tools, ctx, cancelled)

    assert missing.content["status"] == "appointment_not_found"
    assert done.content["status"] == "appointment_not_active"


async def test_repetir_a_confirmacao_nao_cancela_duas_vezes(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    released: list[SlotReleased] = []

    async def hook(event: SlotReleased) -> None:
        released.append(event)

    old = insert_appointment(sync_engine, seed, starts=FAR)
    tools = tools_for(settings, on_slot_released=hook)
    token = (await ask(tools, ctx_for(seed, config), old)).content["confirmation_token"]
    later = ctx_for(seed, config, now=ANSWERED)

    first = await confirm(tools, later, old, token)
    second = await confirm(tools, later, old, token)

    assert first.content["status"] == second.content["status"] == "cancelled"
    assert len(released) == 1  # a vaga vagou uma vez so


# ----------------------------- cancelamento: calendario e fila -----------------------------


async def test_cancelar_apaga_o_evento_do_profissional(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    give_calendar(sync_engine, seed.ana, "ana@cal")
    old = insert_appointment(sync_engine, seed, starts=FAR, event_id="evt-old")
    calendar = FakeCalendar()
    calendar.put("ana@cal", "evt-old", FAR)
    tools = tools_for(settings, resolver_of(calendar))
    token = (await ask(tools, ctx_for(seed, config), old)).content["confirmation_token"]

    await confirm(tools, ctx_for(seed, config, now=ANSWERED), old, token)

    assert calendar.calls == [("delete", "ana@cal", "evt-old")]
    assert calendar.events == {}
    row = appt(sync_engine, old)
    assert (row.status, row.sync_status, row.external_event_id) == ("cancelled", "synced", None)


async def test_calendario_fora_do_ar_nao_desfaz_o_cancelamento(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """O cliente pediu e o banco manda (D-57): o evento fantasma fica para o reprocessamento."""
    give_calendar(sync_engine, seed.ana, "ana@cal")
    old = insert_appointment(sync_engine, seed, starts=FAR, event_id="evt-old")
    calendar = FakeCalendar()
    calendar.put("ana@cal", "evt-old", FAR)
    calendar.delete_errors["ana@cal"] = CalendarSyncPending("google 503", attempts=5)
    tools = tools_for(settings, resolver_of(calendar))
    token = (await ask(tools, ctx_for(seed, config), old)).content["confirmation_token"]

    result = await confirm(tools, ctx_for(seed, config, now=ANSWERED), old, token)

    assert result.content["status"] == "cancelled"
    row = appt(sync_engine, old)
    assert (row.status, row.sync_status, row.external_event_id) == (
        "cancelled",
        "pending_sync",
        "evt-old",
    )


async def test_cancelar_sem_credencial_do_calendario_marca_sync_failed(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    give_calendar(sync_engine, seed.ana, "ana@cal")
    old = insert_appointment(sync_engine, seed, starts=FAR, event_id="evt-old")
    tools = tools_for(settings, no_calendars)
    token = (await ask(tools, ctx_for(seed, config), old)).content["confirmation_token"]

    result = await confirm(tools, ctx_for(seed, config, now=ANSWERED), old, token)

    assert result.content["status"] == "cancelled"
    assert appt(sync_engine, old).sync_status == "sync_failed"


async def test_lista_de_espera_ligada_avisa_que_o_horario_vagou(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    released: list[SlotReleased] = []

    async def hook(event: SlotReleased) -> None:
        released.append(event)

    assert config.scheduling.waitlist_enabled is True
    old = insert_appointment(sync_engine, seed, starts=FAR)
    tools = tools_for(settings, on_slot_released=hook)
    token = (await ask(tools, ctx_for(seed, config), old)).content["confirmation_token"]

    await confirm(tools, ctx_for(seed, config, now=ANSWERED), old, token)

    assert released == [
        SlotReleased(
            tenant_id=seed.tenant,
            provider_id=seed.ana,
            resource_id=None,
            service_id=seed.consulta,
            starts_at=FAR.astimezone(UTC),
            ends_at=(FAR + timedelta(minutes=30)).astimezone(UTC),
        )
    ]


async def test_lista_de_espera_desligada_nao_avisa_nem_na_pergunta(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    released: list[SlotReleased] = []

    async def hook(event: SlotReleased) -> None:
        released.append(event)

    off = with_scheduling(config, waitlist_enabled=False)
    old = insert_appointment(sync_engine, seed, starts=FAR)
    tools = tools_for(settings, on_slot_released=hook)
    token = (await ask(tools, ctx_for(seed, off), old)).content["confirmation_token"]
    await confirm(tools, ctx_for(seed, off, now=ANSWERED), old, token)

    assert released == []


async def test_encaixe_que_falha_nao_desfaz_o_cancelamento(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    async def broken(event: SlotReleased) -> None:
        raise RuntimeError("fila fora do ar")

    old = insert_appointment(sync_engine, seed, starts=FAR)
    tools = tools_for(settings, on_slot_released=broken)
    token = (await ask(tools, ctx_for(seed, config), old)).content["confirmation_token"]

    result = await confirm(tools, ctx_for(seed, config, now=ANSWERED), old, token)

    assert result.content["status"] == "cancelled"
    assert appt(sync_engine, old).status == "cancelled"


async def test_cancelar_e_isolado_por_tenant(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    mine = insert_appointment(sync_engine, seed, starts=FAR)
    stranger = seed_booking(sync_engine)
    try:
        result = await ask(
            tools_for(settings), ctx_for(stranger, config, contact=seed.contact), mine
        )
        assert result.content["status"] == "appointment_not_found"
    finally:
        drop_tenant(sync_engine, stranger.tenant)


# ================================ o motor inteiro, com o modelo ================================


def engine_for(
    settings: Settings, provider: Any, calendars: CalendarResolver = no_calendars
) -> AgentEngine:
    return AgentEngine(
        provider=provider,
        tools=build_registry(settings=settings, calendars=calendars),
        audit=NullAuditSink(),
        extract=False,
        output_guardrail=OutputGuardrail(),
    )


def turn_for(
    seed: BookingSeed, config: TenantConfig, text: str, *, now: datetime = NOW
) -> TurnRequest:
    return TurnRequest(
        tenant_id=seed.tenant,
        conversation_id=seed.conversations[0],
        contact_id=seed.contact,
        channel="whatsapp",
        config=config,
        now=now,
        state=ConversationState(history=(StoredMessage("inbound", "contact", text),)),
    )


def last_result(messages: Sequence[LLMMessage]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if isinstance(message.content, tuple):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    decoded = json.loads(block.content)
                    assert isinstance(decoded, dict)
                    return decoded
    return None


@dataclass
class RescheduleFlow:
    """Um modelo que faz o certo: acha o agendamento, consulta o mesmo servico e remarca."""

    steps: list[str] = field(default_factory=list)
    appointment: dict[str, Any] = field(default_factory=dict)

    async def complete(
        self,
        *,
        system: Sequence[PromptSegment],
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSchema] = (),
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        last = last_result(messages)
        if last is None:
            self.steps.append("find_appointments")
            return tool_response("find_appointments", {})
        if "appointments" in last:
            self.appointment = last["appointments"][0]
            self.steps.append("check_availability")
            return tool_response(
                "check_availability",
                {
                    "service_id": self.appointment["service_id"],
                    "date_from": WED.isoformat(),
                    "date_to": WED.isoformat(),
                    "provider_id": None,
                    "limit": 1,
                },
            )
        if "slots" in last:
            self.steps.append("reschedule_appointment")
            return tool_response(
                "reschedule_appointment",
                {
                    "appointment_id": self.appointment["appointment_id"],
                    "slot_token": last["slots"][0]["slot_token"],
                },
            )
        self.steps.append("reply")
        return text_response(f"Remarcado! Fica para {last['human']}.")


async def test_fluxo_completo_de_remarcacao_pelo_motor(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)
    model = RescheduleFlow()

    outcome = await engine_for(settings, model).run_turn(
        turn_for(seed, config, "preciso mudar minha consulta pra quarta cedo")
    )

    assert model.steps == [
        "find_appointments",
        "check_availability",
        "reschedule_appointment",
        "reply",
    ]
    assert all(call.ok for call in outcome.tool_calls)
    assert outcome.stage == "booked"
    assert not outcome.escalate
    # A resposta citou data e hora que vieram do resultado da propria tool: nao foi barrada.
    assert outcome.violations == ()
    assert not outcome.regenerated
    assert "quarta-feira, 11 de novembro" in outcome.reply
    assert appt(sync_engine, old).status == "cancelled"
    assert active(sync_engine, seed) == 1


async def test_fora_do_prazo_o_motor_escala_sem_o_modelo_pedir(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=NEAR)
    provider = ScriptedProvider(
        script=[
            tool_response("find_appointments", {}),
            tool_response("reschedule_appointment", {"appointment_id": str(old)}),
            text_response("Como a consulta e daqui a pouco, vou chamar alguem da equipe."),
        ]
    )

    outcome = await engine_for(settings, provider).run_turn(
        turn_for(seed, config, "preciso remarcar minha consulta de hoje")
    )

    assert outcome.used_tools == ("find_appointments", "reschedule_appointment")
    assert "check_availability" not in outcome.used_tools
    assert outcome.escalate is True
    assert outcome.escalation_reason == "policy:remarcacao_fora_do_prazo"
    assert outcome.stage == "handoff"
    assert appt(sync_engine, old).status == "confirmed"


@dataclass
class CancelInOneTurn:
    """O atalho: pergunta e, sem esperar ninguem, usa o token que acabou de receber."""

    appointment_id: str

    async def complete(
        self,
        *,
        system: Sequence[PromptSegment],
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSchema] = (),
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        last = last_result(messages)
        if last is None:
            return tool_response("cancel_appointment", {"appointment_id": self.appointment_id})
        if "confirmation_token" in last:
            return tool_response(
                "cancel_appointment",
                {
                    "appointment_id": self.appointment_id,
                    "confirmation_token": last["confirmation_token"],
                },
            )
        return text_response("Pode confirmar o cancelamento?")


async def test_o_modelo_nao_cancela_em_um_turno_so(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """Aceite da S16, pelo motor: pedir e confirmar no mesmo turno termina em zero cancelamentos."""
    old = insert_appointment(sync_engine, seed, starts=FAR)

    outcome = await engine_for(settings, CancelInOneTurn(str(old))).run_turn(
        turn_for(seed, config, "quero cancelar")
    )

    asked, shortcut = outcome.tool_calls
    assert asked.result["status"] == "confirmation_required"
    assert shortcut.ok is False
    assert shortcut.result["status"] == "confirmation_pending"
    assert appt(sync_engine, old).status == "confirmed"
    assert outcome.stage != "booked"


async def test_cancelamento_em_dois_turnos_pelo_motor(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=FAR)
    first = await engine_for(
        settings,
        ScriptedProvider(
            script=[
                tool_response("find_appointments", {}),
                tool_response("cancel_appointment", {"appointment_id": str(old)}),
                text_response(
                    "Confirma o cancelamento da consulta de quarta-feira, 11 de novembro, às 14h?"
                ),
            ]
        ),
    ).run_turn(turn_for(seed, config, "quero cancelar minha consulta"))

    assert first.violations == ()
    assert appt(sync_engine, old).status == "confirmed"
    token = first.tool_calls[1].result["confirmation_token"]

    second = await engine_for(
        settings,
        ScriptedProvider(
            script=[
                tool_response(
                    "cancel_appointment",
                    {"appointment_id": str(old), "confirmation_token": token},
                ),
                text_response(
                    "Cancelado! A consulta de quarta-feira, 11 de novembro, às 14h foi cancelada. "
                    "Quer remarcar?"
                ),
            ]
        ),
    ).run_turn(turn_for(seed, config, "sim, pode cancelar", now=ANSWERED))

    assert second.violations == ()  # a hora citada veio do resultado da tool
    assert second.tool_calls[0].ok
    assert second.stage == "answering"
    assert not second.escalate
    assert appt(sync_engine, old).status == "cancelled"


async def test_cancelar_fora_do_prazo_o_motor_escala(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    old = insert_appointment(sync_engine, seed, starts=NEAR)
    provider = ScriptedProvider(
        script=[
            tool_response("find_appointments", {}),
            tool_response("cancel_appointment", {"appointment_id": str(old)}),
            text_response("Vou pedir para alguem da equipe cuidar disso para voce."),
        ]
    )

    outcome = await engine_for(settings, provider).run_turn(
        turn_for(seed, config, "cancela minha consulta de hoje")
    )

    assert outcome.escalate is True
    assert outcome.escalation_reason == "policy:cancelamento_fora_do_prazo"
    assert appt(sync_engine, old).status == "confirmed"
