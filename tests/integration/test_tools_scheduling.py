"""Tools de agendamento contra Postgres real (secao 11.4; aceite da Sessao S15).

Tres blocos, na ordem do aceite:

1. `slot_token`: `hold_slot` recusa token adulterado, inventado, de outra conversa ou vencido,
   e a recusa acontece **antes** do banco — nenhuma linha em `holds`.
2. `confirm_appointment` com hold expirado devolve `{"status": "hold_expired"}` com
   `suggested_action`, e nao cria agendamento.
3. O modelo nao agenda horario que nao veio de `check_availability`: provado pelo motor
   inteiro, com um modelo roteirizado que tenta o atalho.

O resto cobre o que cada tool faz de verdade: oferta, reserva idempotente, confirmacao com
espelho no calendario, listagem e gravacao do contato.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from app.agent.audit import NullAuditSink
from app.agent.engine import AgentEngine, ConversationState, TurnRequest
from app.agent.guardrails import OutputGuardrail
from app.agent.llm import LLMMessage, LLMResponse, PromptSegment, ToolResultBlock, ToolSchema
from app.agent.memory import StoredMessage
from app.agent.temporal import evidence_from_tool_result
from app.agent.tools.base import ToolContext, ToolError, ToolSpec
from app.agent.tools.check_availability import check_availability_tool
from app.agent.tools.common import slot_signer
from app.agent.tools.confirm_appointment import confirm_appointment_tool
from app.agent.tools.find_appointments import find_appointments_tool
from app.agent.tools.hold_slot import hold_slot_tool
from app.agent.tools.registry import build_registry
from app.agent.tools.save_contact_info import save_contact_info_tool
from app.calendar.base import (
    CalendarProvider,
    CalendarSyncPending,
    EventDraft,
    EventPatch,
    ExternalEvent,
    Interval,
    SyncResult,
)
from app.calendar.resolver import no_calendars
from app.core.config import Settings
from app.core.db import dispose_engine
from app.core.time import combine_local
from app.domain.models import Provider, Resource
from app.domain.tenant_config import TenantConfig, load_tenant_config
from app.scheduling.availability import CalendarResolver
from tests.conftest import TENANTS_DIR
from tests.fakes import ScriptedProvider, text_response, tool_response
from tests.integration.booking_seed import BookingSeed, drop_tenant, seed_booking

pytestmark = pytest.mark.integration

SP = "America/Sao_Paulo"
TUE = date(2026, 11, 10)
WED = date(2026, 11, 11)
SUN = date(2026, 11, 15)
MON = date(2026, 11, 16)
NOW = combine_local(TUE, time(10, 0), SP)

_ISO_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")


# ------------------------------------------- base -------------------------------------------


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


def ctx_for(
    seed: BookingSeed,
    config: TenantConfig,
    *,
    conversation: int = 0,
    now: datetime = NOW,
    contact: uuid.UUID | None = None,
    collected: Mapping[str, Any] | None = None,
) -> ToolContext:
    return ToolContext(
        tenant_id=seed.tenant,
        conversation_id=seed.conversations[conversation],
        contact_id=contact or seed.contact,
        channel="whatsapp",
        config=config,
        now=now,
        collected=collected or {},
    )


def rows(engine: sa.Engine, sql: str, **params: object) -> list[sa.Row[Any]]:
    with engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        return list(conn.execute(sa.text(sql), params).all())


def count(engine: sa.Engine, table: str, tenant: uuid.UUID) -> int:
    return int(rows(engine, f"SELECT count(*) FROM {table} WHERE tenant_id = :t", t=tenant)[0][0])


class FakeCalendar:
    """Calendario com estado: o que se afirma e sobre os eventos, nao sobre chamadas soltas."""

    def __init__(self) -> None:
        self.events: dict[str, EventDraft] = {}
        self.create_calls = 0
        self.create_error: Exception | None = None
        self.busy_error: Exception | None = None

    async def get_busy(
        self, calendar_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, list[Interval]]:
        if self.busy_error is not None:
            raise self.busy_error
        return {calendar_id: [] for calendar_id in calendar_ids}

    async def create_event(
        self, calendar_id: str, event: EventDraft, idempotency_key: str
    ) -> ExternalEvent:
        self.create_calls += 1
        if self.create_error is not None:
            raise self.create_error
        self.events[idempotency_key] = event
        return ExternalEvent(
            id=f"evt-{idempotency_key[:8]}",
            status="confirmed",
            start=event.start,
            end=event.end,
            ical_uid=idempotency_key,
        )

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


def resolver_of(calendar: CalendarProvider | None) -> CalendarResolver:
    def resolve(owner: Provider | Resource) -> CalendarProvider | None:
        return calendar

    return resolve


@dataclass
class Tools:
    check: ToolSpec
    hold: ToolSpec
    confirm: ToolSpec
    find: ToolSpec
    save: ToolSpec


def tools_for(settings: Settings, calendars: CalendarResolver = no_calendars) -> Tools:
    return Tools(
        check=check_availability_tool(calendars, settings=settings),
        hold=hold_slot_tool(settings=settings),
        confirm=confirm_appointment_tool(calendars, settings=settings),
        find=find_appointments_tool(settings=settings),
        save=save_contact_info_tool(settings=settings),
    )


def give_ana_a_calendar(engine: sa.Engine, seed: BookingSeed) -> None:
    sql = "UPDATE providers SET calendar_id = 'ana@cal' WHERE id = :p RETURNING id"
    assert rows(engine, sql, p=seed.ana)


def window(day: date = WED, **extra: Any) -> dict[str, Any]:
    return {"date_from": day.isoformat(), "date_to": day.isoformat(), **extra}


async def offer(tools: Tools, ctx: ToolContext, service: uuid.UUID, **extra: Any) -> dict[str, Any]:
    result = await tools.check.handler(ctx, {"service_id": str(service), **window(**extra)})
    assert not result.is_error, result.content
    return dict(result.content)


async def hold_first(
    tools: Tools, ctx: ToolContext, service: uuid.UUID, **extra: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Oferece, reserva o primeiro horario. Devolve (slot, resultado do hold)."""
    slot = (await offer(tools, ctx, service, **extra))["slots"][0]
    result = await tools.hold.handler(ctx, {"slot_token": slot["slot_token"]})
    assert not result.is_error, result.content
    return slot, dict(result.content)


def iso_instants(content: Mapping[str, Any]) -> list[str]:
    return _ISO_INSTANT.findall(json.dumps(content))


def local_hhmm(starts_at: str) -> str:
    return datetime.fromisoformat(starts_at).strftime("%H:%M")


# ====================================== check_availability ======================================


async def test_oferece_o_padrao_do_tenant_com_token_assinado(
    seed: BookingSeed, config: TenantConfig, settings: Settings
) -> None:
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    content = await offer(tools, ctx, seed.consulta)

    slots = content["slots"]
    assert len(slots) == config.scheduling.slots_per_offer == 3
    assert content["has_more"] is True
    assert content["next_available_date"] == WED.isoformat()

    signer = slot_signer(settings)
    for slot in slots:
        assert set(slot) == {
            "slot_token",
            "starts_at",
            "provider_id",
            "provider_name",
            "duration_minutes",
            "human",
        }
        claims = signer.verify(
            slot["slot_token"],
            tenant_id=seed.tenant,
            conversation_id=seed.conversations[0],
            now=NOW,
        )
        assert claims.service_id == seed.consulta
        assert claims.starts_at == datetime.fromisoformat(slot["starts_at"])
        assert str(claims.provider_id) == slot["provider_id"]
        assert slot["duration_minutes"] == 30
        assert slot["provider_name"] in {"Ana", "Bruno"}

    first = slots[0]
    assert local_hhmm(first["starts_at"]) == "08:00"
    assert first["human"] == "quarta-feira, 11 de novembro, às 8h"


async def test_limit_nao_passa_do_padrao_do_tenant(
    seed: BookingSeed, config: TenantConfig, settings: Settings
) -> None:
    """RF-17: no maximo N por vez. O schema deixa pedir 10; o tenant diz 3."""
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)

    assert len((await offer(tools, ctx, seed.consulta, limit=10))["slots"]) == 3
    two = await offer(tools, ctx, seed.consulta, limit=2)
    assert len(two["slots"]) == 2
    assert two["has_more"] is True


async def test_mais_horarios_e_offset_sobre_a_mesma_ordenacao(
    seed: BookingSeed, config: TenantConfig, settings: Settings
) -> None:
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)

    first = (await offer(tools, ctx, seed.consulta))["slots"]
    second = (await offer(tools, ctx, seed.consulta, offset=3))["slots"]

    starts_first = [slot["starts_at"] for slot in first]
    starts_second = [slot["starts_at"] for slot in second]
    assert len(second) == 3
    assert not set(starts_first) & set(starts_second)
    assert max(starts_first) < min(starts_second)


async def test_o_resultado_so_prova_o_inicio_do_atendimento(
    seed: BookingSeed, config: TenantConfig, settings: Settings
) -> None:
    """Fim do atendimento nao pode virar "horario oferecivel" para o guardrail de saida."""
    tools = tools_for(settings)
    content = await offer(tools, ctx_for(seed, config), seed.consulta)

    starts = sorted(slot["starts_at"] for slot in content["slots"])
    assert sorted(iso_instants(content)) == starts
    assert "ends_at" not in json.dumps(content)

    evidence = evidence_from_tool_result(content, timezone=SP, today=TUE)
    offered = {(datetime.fromisoformat(s).date(), datetime.fromisoformat(s).time()) for s in starts}
    assert evidence.slots == offered
    # 09:00 e o fim do atendimento das 08:30 (o ultimo oferecido): nao e horario oferecido.
    assert (WED, time(9, 0)) not in evidence.slots


async def test_preferencia_de_periodo_ordena_a_oferta(
    seed: BookingSeed, config: TenantConfig, settings: Settings
) -> None:
    content = await offer(
        tools_for(settings), ctx_for(seed, config), seed.consulta, preferred_periods=["afternoon"]
    )

    assert all(local_hhmm(s["starts_at"]) >= "13:30" for s in content["slots"])


async def test_servico_com_recurso_leva_o_recurso_no_token(
    seed: BookingSeed, config: TenantConfig, settings: Settings
) -> None:
    content = await offer(tools_for(settings), ctx_for(seed, config), seed.banho)

    claims = slot_signer(settings).verify(
        content["slots"][0]["slot_token"],
        tenant_id=seed.tenant,
        conversation_id=seed.conversations[0],
        now=NOW,
    )
    assert claims.resource_id == seed.room
    assert claims.service_id == seed.banho


async def test_janela_vazia_aponta_o_proximo_dia_com_vaga(
    seed: BookingSeed, config: TenantConfig, settings: Settings
) -> None:
    content = await offer(tools_for(settings), ctx_for(seed, config), seed.consulta, day=SUN)

    assert content["slots"] == []
    assert content["has_more"] is False
    assert content["next_available_date"] == MON.isoformat()


async def test_sem_profissional_apto_nao_ha_proximo_dia(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    orphan = rows(
        sync_engine,
        "INSERT INTO services (tenant_id, name, duration_minutes) "
        "VALUES (:t, 'Sem ninguem', 30) RETURNING id",
        t=seed.tenant,
    )[0][0]

    content = await offer(tools_for(settings), ctx_for(seed, config), orphan)

    assert content["slots"] == []
    assert content["next_available_date"] is None


async def test_servico_inexistente_manda_consultar_o_catalogo(
    seed: BookingSeed, config: TenantConfig, settings: Settings
) -> None:
    result = await tools_for(settings).check.handler(
        ctx_for(seed, config), {"service_id": str(uuid.uuid4()), **window()}
    )

    assert result.is_error
    assert result.content["status"] == "unknown_service"
    assert "list_services" in result.content["suggested_action"]


async def test_servico_de_outro_tenant_nao_existe(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    other = seed_booking(sync_engine)
    try:
        result = await tools_for(settings).check.handler(
            ctx_for(seed, config), {"service_id": str(other.consulta), **window()}
        )
        assert result.is_error
        assert result.content["status"] == "unknown_service"
    finally:
        drop_tenant(sync_engine, other.tenant)


async def test_calendario_que_nao_responde_marca_a_oferta_como_incompleta(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    give_ana_a_calendar(sync_engine, seed)
    calendar = FakeCalendar()
    calendar.busy_error = CalendarSyncPending("google fora do ar: 503 interno", attempts=5)
    tools = tools_for(settings, resolver_of(calendar))

    content = await offer(tools, ctx_for(seed, config), seed.consulta, limit=3)

    # Ana saiu da conta; Bruno, sem calendario externo, continua ofertando.
    assert {slot["provider_name"] for slot in content["slots"]} == {"Bruno"}
    assert content["incomplete"] is True
    assert "nao diga" in content["note"].lower() or "nao ha vaga" in content["note"].lower()
    # O motivo tecnico fica no log: nao vai para o prompt.
    assert "503" not in json.dumps(content)
    assert "ana@cal" not in json.dumps(content)


async def test_calendario_sem_credencial_tambem_tira_o_profissional(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    give_ana_a_calendar(sync_engine, seed)

    content = await offer(
        tools_for(settings, no_calendars),
        ctx_for(seed, config),
        seed.consulta,
        provider_id=str(seed.ana),
    )

    assert content["slots"] == []
    assert content["incomplete"] is True


# ========================================= hold_slot =========================================


async def test_reserva_o_horario_ofertado(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)

    slot, held = await hold_first(tools, ctx, seed.consulta)

    assert held["status"] == "held"
    assert held["starts_at"] == slot["starts_at"]
    assert held["human"] == slot["human"]
    assert held["expires_in_minutes"] == config.scheduling.hold_ttl_minutes
    stored = rows(
        sync_engine,
        "SELECT status, provider_id, service_id, starts_at, conversation_id "
        "FROM holds WHERE id = :h",
        h=uuid.UUID(held["hold_id"]),
    )[0]
    assert stored.status == "active"
    assert stored.service_id == seed.consulta
    assert stored.conversation_id == seed.conversations[0]
    assert stored.starts_at == datetime.fromisoformat(slot["starts_at"])


async def test_o_hold_nao_expoe_instante_que_pareca_horario(
    seed: BookingSeed, config: TenantConfig, settings: Settings
) -> None:
    """`expires_at` as 08:10 viraria prova de uma vaga as 08:10 (`app.agent.tools.common`)."""
    slot, held = await hold_first(tools_for(settings), ctx_for(seed, config), seed.consulta)

    assert iso_instants(held) == [slot["starts_at"]]
    assert "expires_at" not in held
    assert "ends_at" not in held


async def test_token_adulterado_e_rejeitado_antes_do_banco(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    token = (await offer(tools, ctx, seed.consulta))["slots"][0]["slot_token"]
    payload, signature = token.split(".")
    flipped = "A" if payload[5] != "A" else "B"
    tampered = f"{payload[:5]}{flipped}{payload[6:]}.{signature}"

    result = await tools.hold.handler(ctx, {"slot_token": tampered})

    assert result.is_error
    assert result.content["status"] == "invalid_slot_token"
    assert result.content["suggested_action"] == "call check_availability again"
    assert count(sync_engine, "holds", seed.tenant) == 0


async def test_token_inventado_pelo_modelo_nao_reserva_nada(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """O que o modelo faria: o corpo certo para um horario que ninguem ofereceu, sem a chave."""
    body = {
        "v": 1,
        "p": seed.ana.hex,
        "s": seed.consulta.hex,
        "r": None,
        "a": datetime(2026, 11, 11, 18, 0, tzinfo=UTC).isoformat(),
        "e": int((NOW + timedelta(days=1)).timestamp()),
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    forged = (
        base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        + "."
        + base64.urlsafe_b64encode(b"\x00" * 32).rstrip(b"=").decode()
    )

    result = await tools_for(settings).hold.handler(ctx_for(seed, config), {"slot_token": forged})

    assert result.is_error
    assert result.content["status"] == "invalid_slot_token"
    assert count(sync_engine, "holds", seed.tenant) == 0


async def test_token_de_outra_conversa_nao_vale(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    tools = tools_for(settings)
    token = (await offer(tools, ctx_for(seed, config, conversation=0), seed.consulta))["slots"][0][
        "slot_token"
    ]

    result = await tools.hold.handler(ctx_for(seed, config, conversation=1), {"slot_token": token})

    assert result.is_error
    assert result.content["status"] == "invalid_slot_token"
    assert count(sync_engine, "holds", seed.tenant) == 0


async def test_oferta_vencida_nao_reserva(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    tools = tools_for(settings)
    token = (await offer(tools, ctx_for(seed, config), seed.consulta))["slots"][0]["slot_token"]
    later = NOW + timedelta(minutes=settings.slot_token_ttl_minutes, seconds=1)

    result = await tools.hold.handler(ctx_for(seed, config, now=later), {"slot_token": token})

    assert result.is_error
    assert result.content["status"] == "invalid_slot_token"
    assert count(sync_engine, "holds", seed.tenant) == 0


async def test_reservar_de_novo_o_mesmo_horario_devolve_a_mesma_reserva(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """O cliente responde "pode ser esse" depois de o modelo ja ter reservado."""
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    slot = (await offer(tools, ctx, seed.consulta))["slots"][0]

    first = await tools.hold.handler(ctx, {"slot_token": slot["slot_token"]})
    later_ctx = ctx_for(seed, config, now=NOW + timedelta(minutes=3))
    second = await tools.hold.handler(later_ctx, {"slot_token": slot["slot_token"]})

    assert not first.is_error and not second.is_error
    assert first.content["hold_id"] == second.content["hold_id"]
    assert count(sync_engine, "holds", seed.tenant) == 1
    # A repeticao nao estende a reserva: o relogio dela continua o do primeiro pedido.
    assert second.content["expires_in_minutes"] == first.content["expires_in_minutes"] - 3


async def test_reservar_outro_horario_libera_o_anterior(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    slots = (await offer(tools, ctx, seed.consulta))["slots"]

    first = await tools.hold.handler(ctx, {"slot_token": slots[0]["slot_token"]})
    # 08:15 se sobrepoe ao 08:00 que a propria conversa reservou: a troca nao pode travar nisso.
    second = await tools.hold.handler(ctx, {"slot_token": slots[1]["slot_token"]})

    assert not first.is_error
    assert not second.is_error, second.content
    status = {
        str(row.id): row.status
        for row in rows(
            sync_engine, "SELECT id, status FROM holds WHERE tenant_id = :t", t=seed.tenant
        )
    }
    assert status[first.content["hold_id"]] == "expired"
    assert status[second.content["hold_id"]] == "active"


async def test_troca_que_falha_nao_derruba_a_reserva_que_a_pessoa_ja_tinha(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    tools = tools_for(settings)
    mine = ctx_for(seed, config, conversation=0)
    theirs = ctx_for(seed, config, conversation=1)
    ana = {"provider_id": str(seed.ana), "limit": 1}
    keep = (await offer(tools, mine, seed.consulta, **ana))["slots"][0]  # 08:00
    wanted = (await offer(tools, mine, seed.consulta, offset=4, **ana))["slots"][0]  # 09:00
    taken = (await offer(tools, theirs, seed.consulta, offset=4, **ana))["slots"][0]
    assert wanted["starts_at"] == taken["starts_at"]

    kept = await tools.hold.handler(mine, {"slot_token": keep["slot_token"]})
    await tools.hold.handler(theirs, {"slot_token": taken["slot_token"]})
    swap = await tools.hold.handler(mine, {"slot_token": wanted["slot_token"]})

    assert swap.is_error
    assert swap.content["status"] == "slot_taken"
    status = rows(
        sync_engine, "SELECT status FROM holds WHERE id = :h", h=uuid.UUID(kept.content["hold_id"])
    )[0].status
    assert status == "active"


async def test_horario_que_outra_conversa_reservou_primeiro_e_slot_taken(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    tools = tools_for(settings)
    mine = ctx_for(seed, config, conversation=0)
    theirs = ctx_for(seed, config, conversation=1)
    wanted = (await offer(tools, mine, seed.consulta, provider_id=str(seed.ana)))["slots"][0]
    theirs_token = (await offer(tools, theirs, seed.consulta, provider_id=str(seed.ana)))["slots"][
        0
    ]
    assert theirs_token["starts_at"] == wanted["starts_at"]

    first = await tools.hold.handler(theirs, {"slot_token": theirs_token["slot_token"]})
    result = await tools.hold.handler(mine, {"slot_token": wanted["slot_token"]})

    assert not first.is_error
    assert result.is_error
    assert result.content["status"] == "slot_taken"
    assert result.content["suggested_action"] == "call check_availability again"
    assert count(sync_engine, "holds", seed.tenant) == 1


async def test_o_recurso_do_servico_vai_no_hold(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    _, held = await hold_first(tools_for(settings), ctx_for(seed, config), seed.banho)

    stored = rows(
        sync_engine, "SELECT resource_id FROM holds WHERE id = :h", h=uuid.UUID(held["hold_id"])
    )[0]
    assert stored.resource_id == seed.room


# ===================================== confirm_appointment =====================================


async def book(
    tools: Tools,
    ctx: ToolContext,
    service: uuid.UUID,
    *,
    arguments: Mapping[str, Any] | None = None,
    **offer_extra: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Oferece, reserva e confirma. Devolve (hold, confirmacao)."""
    _, held = await hold_first(tools, ctx, service, **offer_extra)
    result = await tools.confirm.handler(ctx, {"hold_id": held["hold_id"], **(arguments or {})})
    return held, dict(result.content)


async def test_confirma_e_devolve_os_dados_do_agendamento(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)

    held, content = await book(
        tools,
        ctx,
        seed.consulta,
        arguments={
            "subject_name": "Mia",
            "intake": {"species": "gato", "campo_inventado": "x"},
            "notes": "tem medo de barulho",
        },
    )

    assert content["status"] == "confirmed"
    assert content["service"] == "Consulta"
    assert content["subject_name"] == "Mia"
    assert content["provider_name"] in {"Ana", "Bruno"}
    assert content["address"] == config.identity.address
    assert content["human"] == "quarta-feira, 11 de novembro, às 8h"
    assert iso_instants(content) == [content["starts_at"]]

    message = content["confirmation_message"]
    assert "*Consulta* para *Mia*" in message
    assert "quarta-feira, 11 de novembro as 8h" in message
    assert config.identity.address in message
    # Servico sem preco e sem preparo: a frase some, o placeholder nao sai literal.
    assert "{" not in message
    assert "R$" not in message

    stored = rows(
        sync_engine,
        "SELECT status, source, sync_status, intake, conversation_id, provider_id "
        "FROM appointments WHERE id = :a",
        a=uuid.UUID(content["appointment_id"]),
    )[0]
    assert stored.status == "confirmed"
    assert stored.source == "agent"
    assert stored.conversation_id == seed.conversations[0]
    assert stored.sync_status == "synced"  # agenda propria: nada a espelhar
    assert stored.intake == {
        "species": "gato",
        "subject_name": "Mia",
        "_notes": "tem medo de barulho",
    }
    assert (
        rows(sync_engine, "SELECT status FROM holds WHERE id = :h", h=uuid.UUID(held["hold_id"]))[
            0
        ].status
        == "consumed"
    )


async def test_preco_e_preparo_do_servico_entram_na_confirmacao(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    rows(
        sync_engine,
        "UPDATE services SET price_cents = 18000, "
        "prep_instructions = 'Leve a carteira de vacinacao.' WHERE id = :s RETURNING id",
        s=seed.consulta,
    )

    _, content = await book(tools_for(settings), ctx_for(seed, config), seed.consulta)

    assert content["price_line"] == "R$ 180,00"
    assert content["prep_line"] == "Leve a carteira de vacinacao."
    assert "R$ 180,00" in content["confirmation_message"]
    assert "Leve a carteira de vacinacao." in content["confirmation_message"]


async def test_confirmacao_com_um_campo_por_linha_so_perde_a_linha_vazia(
    seed: BookingSeed, config: TenantConfig, settings: Settings
) -> None:
    """Template escrito com `|`: o comportamento da D-38, linha a linha."""
    per_line = "Prontinho!\n*{service}*\n{date_human} as {time}\n{price_line}\n{prep_line}"
    custom = config.model_copy(
        update={"messages": config.messages.model_copy(update={"confirmation": per_line})}
    )

    _, content = await book(tools_for(settings), ctx_for(seed, custom), seed.consulta)

    assert content["confirmation_message"] == (
        "Prontinho!\n*Consulta*\nquarta-feira, 11 de novembro as 8h"
    )


async def test_confirmacao_dobrada_numa_linha_nao_perde_data_e_hora(
    seed: BookingSeed, config: TenantConfig, settings: Settings
) -> None:
    """Template escrito com `>` (o do tenant de exemplo): uma frase so, sem preco nem preparo.

    Sem o refazimento, a rede de seguranca descartaria a frase inteira e a pessoa leria
    "Prontinho!" sem saber quando e o atendimento.
    """
    assert "\n" not in config.messages.confirmation.strip()

    _, content = await book(tools_for(settings), ctx_for(seed, config), seed.consulta)

    message = content["confirmation_message"]
    assert message != "Prontinho!"
    assert "quarta-feira, 11 de novembro as 8h" in message
    assert "Se precisar mudar" in message
    assert "  " not in message  # o buraco do preco nao deixa espaco duplo


async def test_servico_online_nao_leva_endereco(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    rows(
        sync_engine,
        "UPDATE services SET modality = 'online' WHERE id = :s RETURNING id",
        s=seed.consulta,
    )

    _, content = await book(tools_for(settings), ctx_for(seed, config), seed.consulta)

    assert content["address"] == ""
    assert config.identity.address not in content["confirmation_message"]


async def test_hold_expirado_devolve_hold_expired_com_suggested_action(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """Aceite da S15."""
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    _, held = await hold_first(tools, ctx, seed.consulta)
    ttl = timedelta(minutes=config.scheduling.hold_ttl_minutes)

    late = ctx_for(seed, config, now=NOW + ttl + timedelta(minutes=1))
    result = await tools.confirm.handler(late, {"hold_id": held["hold_id"]})

    assert result.is_error
    assert result.content["status"] == "hold_expired"
    assert result.content["message"]
    assert result.content["suggested_action"] == "call check_availability again"
    assert count(sync_engine, "appointments", seed.tenant) == 0


async def test_hold_inexistente_ou_de_outra_conversa_e_hold_not_found(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    tools = tools_for(settings)
    mine = ctx_for(seed, config, conversation=0)
    _, held = await hold_first(tools, mine, seed.consulta)

    made_up = await tools.confirm.handler(mine, {"hold_id": str(uuid.uuid4())})
    stolen = await tools.confirm.handler(
        ctx_for(seed, config, conversation=1), {"hold_id": held["hold_id"]}
    )

    for result in (made_up, stolen):
        assert result.is_error
        assert result.content["status"] == "hold_not_found"
    assert count(sync_engine, "appointments", seed.tenant) == 0
    # A reserva de quem a fez continua de pe: a tentativa alheia nao a gastou.
    again = await tools.confirm.handler(mine, {"hold_id": held["hold_id"]})
    assert again.content["status"] == "confirmed"


async def test_repetir_a_confirmacao_devolve_o_mesmo_agendamento(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    calendar = FakeCalendar()
    give_ana_a_calendar(sync_engine, seed)
    tools = tools_for(settings, resolver_of(calendar))
    ctx = ctx_for(seed, config)
    held, first = await book(tools, ctx, seed.consulta, provider_id=str(seed.ana))

    second = await tools.confirm.handler(
        ctx, {"hold_id": held["hold_id"], "notes": "outra observacao"}
    )

    assert second.content["appointment_id"] == first["appointment_id"]
    assert count(sync_engine, "appointments", seed.tenant) == 1
    assert calendar.create_calls == 1  # ja `synced`: a repeticao nao toca o calendario


async def test_horario_tomado_por_fora_na_confirmacao_e_slot_taken(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    slot, held = await hold_first(tools, ctx, seed.consulta, provider_id=str(seed.ana))
    starts = datetime.fromisoformat(slot["starts_at"])
    # Alguem agenda por fora (painel) em cima do hold, sem passar pela reserva.
    rows(
        sync_engine,
        "INSERT INTO appointments (tenant_id, contact_id, provider_id, service_id, starts_at, "
        "ends_at, status) VALUES (:t, :c, :p, :s, :a, :b, 'confirmed') RETURNING id",
        t=seed.tenant,
        c=seed.contact,
        p=seed.ana,
        s=seed.consulta,
        a=starts,
        b=starts + timedelta(minutes=30),
    )

    result = await tools.confirm.handler(ctx, {"hold_id": held["hold_id"]})

    assert result.is_error
    assert result.content["status"] == "slot_taken"
    assert result.content["suggested_action"] == "call check_availability again"
    assert count(sync_engine, "appointments", seed.tenant) == 1  # so o de fora


# ------------------------------------ espelho no calendario ------------------------------------


async def test_cria_o_evento_no_calendario_do_profissional(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    give_ana_a_calendar(sync_engine, seed)
    calendar = FakeCalendar()
    tools = tools_for(settings, resolver_of(calendar))

    _, content = await book(
        tools,
        ctx_for(seed, config),
        seed.consulta,
        arguments={"subject_name": "Mia", "notes": "primeira vez"},
        provider_id=str(seed.ana),
    )

    appointment_id = content["appointment_id"]
    draft = calendar.events[appointment_id]  # iCalUID = appointment_id (13.4)
    assert draft.summary == "Consulta - Mia"
    assert draft.timezone == SP
    assert draft.start == datetime.fromisoformat(content["starts_at"])
    assert draft.end - draft.start == timedelta(minutes=30)
    assert draft.tenant_ref == str(seed.tenant)
    assert draft.description is not None
    assert "Contato: Carla" in draft.description
    assert "Atendido: Mia" in draft.description
    assert "primeira vez" in draft.description
    stored = rows(
        sync_engine,
        "SELECT sync_status, external_event_id FROM appointments WHERE id = :a",
        a=uuid.UUID(appointment_id),
    )[0]
    assert stored.sync_status == "synced"
    assert stored.external_event_id == f"evt-{appointment_id[:8]}"


async def test_calendario_fora_do_ar_nao_desfaz_o_agendamento(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    give_ana_a_calendar(sync_engine, seed)
    calendar = FakeCalendar()
    calendar.create_error = CalendarSyncPending("google 503", attempts=5)
    tools = tools_for(settings, resolver_of(calendar))

    _, content = await book(tools, ctx_for(seed, config), seed.consulta, provider_id=str(seed.ana))

    # O banco e a fonte de verdade: dizer "nao consegui" de uma reserva que existe a duplicaria.
    assert content["status"] == "confirmed"
    stored = rows(
        sync_engine,
        "SELECT sync_status FROM appointments WHERE id = :a",
        a=uuid.UUID(content["appointment_id"]),
    )[0]
    assert stored.sync_status == "pending_sync"


async def test_excecao_inesperada_no_espelho_nao_derruba_a_confirmacao(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    give_ana_a_calendar(sync_engine, seed)
    calendar = FakeCalendar()
    calendar.create_error = RuntimeError("bug no cliente do calendario")
    tools = tools_for(settings, resolver_of(calendar))

    _, content = await book(tools, ctx_for(seed, config), seed.consulta, provider_id=str(seed.ana))

    assert content["status"] == "confirmed"
    assert count(sync_engine, "appointments", seed.tenant) == 1


async def test_calendario_sem_credencial_vira_sync_failed_mas_confirma(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    give_ana_a_calendar(sync_engine, seed)
    # A oferta e feita com calendario; a confirmacao, com o resolver que perdeu a credencial.
    offering = tools_for(settings, resolver_of(FakeCalendar()))
    confirming = tools_for(settings, no_calendars)
    ctx = ctx_for(seed, config)
    _, held = await hold_first(offering, ctx, seed.consulta, provider_id=str(seed.ana))

    result = await confirming.confirm.handler(ctx, {"hold_id": held["hold_id"]})

    assert result.content["status"] == "confirmed"
    stored = rows(
        sync_engine,
        "SELECT sync_status FROM appointments WHERE id = :a",
        a=uuid.UUID(result.content["appointment_id"]),
    )[0]
    assert stored.sync_status == "sync_failed"


async def test_confirmar_de_novo_reenvia_o_evento_que_ficou_pendente(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    give_ana_a_calendar(sync_engine, seed)
    calendar = FakeCalendar()
    calendar.create_error = CalendarSyncPending("google 503", attempts=5)
    tools = tools_for(settings, resolver_of(calendar))
    ctx = ctx_for(seed, config)
    held, first = await book(tools, ctx, seed.consulta, provider_id=str(seed.ana))

    calendar.create_error = None  # o Google voltou
    second = await tools.confirm.handler(ctx, {"hold_id": held["hold_id"]})

    assert second.content["appointment_id"] == first["appointment_id"]
    assert first["appointment_id"] in calendar.events  # idempotente por iCalUID
    stored = rows(
        sync_engine,
        "SELECT sync_status FROM appointments WHERE id = :a",
        a=uuid.UUID(first["appointment_id"]),
    )[0]
    assert stored.sync_status == "synced"


# ----------------------------------- subject e intake -----------------------------------


async def test_subject_cadastrado_e_ligado_ao_agendamento(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    saved = await tools.save.handler(ctx, {"subject": {"kind": "pet", "name": "Mia"}})
    subject_id = uuid.UUID(saved.content["subject"]["subject_id"])

    _, content = await book(tools, ctx, seed.consulta, arguments={"subject_name": "  mia "})

    stored = rows(
        sync_engine,
        "SELECT subject_id FROM appointments WHERE id = :a",
        a=uuid.UUID(content["appointment_id"]),
    )[0]
    assert stored.subject_id == subject_id


async def test_subject_nao_cadastrado_nao_e_inventado(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """O tipo (pet, dependente) so `save_contact_info` recebe; adivinhar gravaria dado errado."""
    _, content = await book(
        tools_for(settings), ctx_for(seed, config), seed.consulta, arguments={"subject_name": "Mia"}
    )

    stored = rows(
        sync_engine,
        "SELECT subject_id, intake FROM appointments WHERE id = :a",
        a=uuid.UUID(content["appointment_id"]),
    )[0]
    assert stored.subject_id is None
    assert stored.intake["subject_name"] == "Mia"
    assert count(sync_engine, "subjects", seed.tenant) == 0


async def test_nome_repetido_entre_subjects_e_ambiguo_e_nao_liga(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    await tools.save.handler(ctx, {"subject": {"kind": "pet", "name": "Mia"}})
    await tools.save.handler(ctx, {"subject": {"kind": "dependent", "name": "Mia"}})

    _, content = await book(tools, ctx, seed.consulta, arguments={"subject_name": "Mia"})

    stored = rows(
        sync_engine,
        "SELECT subject_id FROM appointments WHERE id = :a",
        a=uuid.UUID(content["appointment_id"]),
    )[0]
    assert stored.subject_id is None


async def test_o_que_o_motor_ja_coletou_nao_e_apagado_por_intake_vazio(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    ctx = ctx_for(seed, config, collected={"species": "cachorro", "is_first_visit": True})

    _, content = await book(
        tools_for(settings),
        ctx,
        seed.consulta,
        arguments={"intake": {"species": "", "contact_name": "Carla"}},
    )

    stored = rows(
        sync_engine,
        "SELECT intake FROM appointments WHERE id = :a",
        a=uuid.UUID(content["appointment_id"]),
    )[0]
    assert stored.intake == {"species": "cachorro", "is_first_visit": True, "contact_name": "Carla"}


# ===================================== find_appointments =====================================


def insert_appointment(
    engine: sa.Engine,
    seed: BookingSeed,
    *,
    starts: datetime,
    status: str = "confirmed",
    contact: uuid.UUID | None = None,
    provider: uuid.UUID | None = None,
) -> uuid.UUID:
    found = rows(
        engine,
        "INSERT INTO appointments (tenant_id, contact_id, provider_id, service_id, starts_at, "
        "ends_at, status) VALUES (:t, :c, :p, :s, :a, :b, :st) RETURNING id",
        t=seed.tenant,
        c=contact or seed.contact,
        p=provider or seed.ana,
        s=seed.consulta,
        a=starts,
        b=starts + timedelta(minutes=30),
        st=status,
    )
    value: uuid.UUID = found[0][0]
    return value


async def test_lista_so_agendamento_futuro_ativo_do_contato(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    other_contact = rows(
        sync_engine,
        "INSERT INTO contacts (tenant_id, phone_e164, name) VALUES (:t, :p, 'Outra') RETURNING id",
        t=seed.tenant,
        p=f"+5511{uuid.uuid4().hex[:8]}",
    )[0][0]
    later = insert_appointment(sync_engine, seed, starts=combine_local(WED, time(16, 0), SP))
    sooner = insert_appointment(sync_engine, seed, starts=combine_local(WED, time(9, 0), SP))
    insert_appointment(sync_engine, seed, starts=combine_local(TUE, time(8, 0), SP))  # passado
    insert_appointment(
        sync_engine, seed, starts=combine_local(WED, time(11, 0), SP), status="cancelled"
    )
    insert_appointment(
        sync_engine, seed, starts=combine_local(WED, time(14, 0), SP), contact=other_contact
    )

    result = await tools_for(settings).find.handler(ctx_for(seed, config), {})

    assert not result.is_error
    listed = result.content["appointments"]
    assert [item["appointment_id"] for item in listed] == [str(sooner), str(later)]
    assert result.content["count"] == 2
    first = listed[0]
    assert first["service"] == "Consulta"
    assert first["provider_name"] == "Ana"
    assert first["status"] == "confirmed"
    assert first["human"] == "quarta-feira, 11 de novembro, às 9h"
    assert iso_instants(result.content) == [item["starts_at"] for item in listed]


async def test_sem_agendamento_a_lista_e_vazia(
    seed: BookingSeed, config: TenantConfig, settings: Settings
) -> None:
    result = await tools_for(settings).find.handler(ctx_for(seed, config), {})

    assert result.content == {"appointments": [], "count": 0}


async def test_listagem_mostra_o_nome_de_quem_sera_atendido(
    seed: BookingSeed, config: TenantConfig, settings: Settings
) -> None:
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)
    await tools.save.handler(ctx, {"subject": {"kind": "pet", "name": "Mia"}})
    await book(tools, ctx, seed.consulta, arguments={"subject_name": "Mia"})

    result = await tools.find.handler(ctx, {})

    assert result.content["appointments"][0]["subject_name"] == "Mia"


async def test_outro_tenant_nao_ve_agendamento_alheio(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    insert_appointment(sync_engine, seed, starts=combine_local(WED, time(9, 0), SP))
    stranger = seed_booking(sync_engine)
    try:
        # Contexto do tenant B perguntando pelo contato do tenant A: a RLS responde "nada".
        result = await tools_for(settings).find.handler(
            ctx_for(stranger, config, contact=seed.contact), {}
        )
        assert result.content["appointments"] == []
    finally:
        drop_tenant(sync_engine, stranger.tenant)


# ====================================== save_contact_info ======================================


def contact_row(engine: sa.Engine, seed: BookingSeed) -> sa.Row[Any]:
    return rows(
        engine,
        "SELECT name, email, attributes FROM contacts WHERE id = :c",
        c=seed.contact,
    )[0]


async def test_salva_nome_email_e_mescla_atributos(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)

    first = await tools.save.handler(
        ctx, {"contact_name": "Carla Souza", "attributes": {"prefere_manha": True}}
    )
    await tools.save.handler(ctx, {"email": "carla@example.com", "attributes": {"idade": 31}})

    assert first.content == {"status": "saved", "saved": ["contact_name", "attributes"]}
    stored = contact_row(sync_engine, seed)
    assert stored.name == "Carla Souza"
    assert stored.email == "carla@example.com"
    assert stored.attributes == {"prefere_manha": True, "idade": 31}


async def test_o_mesmo_subject_e_atualizado_nao_duplicado(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)

    first = await tools.save.handler(
        ctx, {"subject": {"kind": "pet", "name": "Mia", "attributes": {"especie": "gato"}}}
    )
    second = await tools.save.handler(
        ctx, {"subject": {"kind": "pet", "name": "MIA", "attributes": {"raca": "SRD"}}}
    )

    assert first.content["subject"]["subject_id"] == second.content["subject"]["subject_id"]
    stored = rows(
        sync_engine,
        "SELECT kind, name, attributes FROM subjects WHERE tenant_id = :t",
        t=seed.tenant,
    )
    assert len(stored) == 1
    assert stored[0].name == "Mia"  # o nome da primeira vez nao e reescrito
    assert stored[0].attributes == {"especie": "gato", "raca": "SRD"}


async def test_mesmo_nome_com_tipo_diferente_e_outro_subject(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    tools = tools_for(settings)
    ctx = ctx_for(seed, config)

    await tools.save.handler(ctx, {"subject": {"kind": "pet", "name": "Mia"}})
    await tools.save.handler(ctx, {"subject": {"kind": "dependent", "name": "Mia"}})

    assert count(sync_engine, "subjects", seed.tenant) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"attributes": {"documento": "123.456.789-09"}},
        {"attributes": {"observacao": "meu cpf e 12345678909"}},
        {"subject": {"kind": "pet", "name": "Mia", "attributes": {"nota": "4111 1111 1111 1111"}}},
        {"contact_name": "Carla 4111 1111 1111 1111"},
    ],
)
async def test_recusa_documento_e_cartao_sem_gravar_nada(
    payload: dict[str, Any],
    seed: BookingSeed,
    config: TenantConfig,
    settings: Settings,
    sync_engine: sa.Engine,
) -> None:
    result = await tools_for(settings).save.handler(ctx_for(seed, config), payload)

    assert result.is_error
    assert result.content["status"] == "sensitive_data"
    stored = contact_row(sync_engine, seed)
    assert stored.attributes == {}
    assert stored.name == "Carla"  # o do seed
    assert count(sync_engine, "subjects", seed.tenant) == 0


async def test_o_contato_e_o_da_conversa_nao_o_do_argumento(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    other = rows(
        sync_engine,
        "INSERT INTO contacts (tenant_id, phone_e164, name) VALUES (:t, :p, 'Outra') RETURNING id",
        t=seed.tenant,
        p=f"+5511{uuid.uuid4().hex[:8]}",
    )[0][0]

    await tools_for(settings).save.handler(
        ctx_for(seed, config), {"contact_name": "Carla Nova", "contact_id": str(other)}
    )

    assert contact_row(sync_engine, seed).name == "Carla Nova"
    untouched = rows(sync_engine, "SELECT name FROM contacts WHERE id = :c", c=other)[0]
    assert untouched.name == "Outra"


async def test_outro_tenant_nao_grava_no_contato_alheio(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    stranger = seed_booking(sync_engine)
    try:
        with pytest.raises(ToolError):
            await tools_for(settings).save.handler(
                ctx_for(stranger, config, contact=seed.contact), {"contact_name": "Invasor"}
            )
        assert contact_row(sync_engine, seed).name == "Carla"
    finally:
        drop_tenant(sync_engine, stranger.tenant)


# ====================== o motor inteiro, com o modelo tentando o atalho ======================


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


def turn_for(seed: BookingSeed, config: TenantConfig) -> TurnRequest:
    return TurnRequest(
        tenant_id=seed.tenant,
        conversation_id=seed.conversations[0],
        contact_id=seed.contact,
        channel="whatsapp",
        config=config,
        now=NOW,
        state=ConversationState(
            history=(StoredMessage("inbound", "contact", "quero marcar uma consulta quarta"),)
        ),
    )


async def test_o_modelo_nao_agenda_horario_que_nao_veio_de_check_availability(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    """Aceite da S15, pelo motor: todos os atalhos do modelo terminam em erro e em zero linhas."""
    chosen = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
    provider = ScriptedProvider(
        script=[
            # 1. Escreve o horario que quer, sem ter consultado nada.
            tool_response(
                "hold_slot",
                {
                    "provider_id": str(seed.ana),
                    "service_id": str(seed.consulta),
                    "starts_at": chosen.isoformat(),
                },
            ),
            # 2. Inventa um token.
            tool_response("hold_slot", {"slot_token": "eyJ2IjoxfQ.assinatura-inventada"}),
            # 3. Pula a reserva e confirma um hold que nunca existiu.
            tool_response(
                "confirm_appointment", {"hold_id": str(uuid.uuid4()), "subject_name": "Mia"}
            ),
            text_response("Agendei para voce, quarta-feira, 11 de novembro, as 12h."),
        ]
    )

    outcome = await engine_for(settings, provider).run_turn(turn_for(seed, config))

    calls = outcome.tool_calls
    assert [call.name for call in calls[:3]] == ["hold_slot", "hold_slot", "confirm_appointment"]
    assert not any(call.ok for call in calls[:3])
    assert calls[0].result["error"] == "argumentos_invalidos"
    assert calls[1].result["status"] == "invalid_slot_token"
    assert calls[2].result["status"] == "hold_not_found"
    assert count(sync_engine, "holds", seed.tenant) == 0
    assert count(sync_engine, "appointments", seed.tenant) == 0
    # Nada deu certo, entao o funil nao anda para "confirming" nem "booked".
    assert outcome.stage not in {"confirming", "booked"}


async def test_modelo_que_cita_horario_fora_da_oferta_e_barrado_pelo_guardrail(
    seed: BookingSeed, config: TenantConfig, settings: Settings
) -> None:
    """O formato do resultado da tool tem de ser lido pelo guardrail de saida (11.5)."""
    provider = ScriptedProvider(
        script=[
            tool_response(
                "check_availability",
                {
                    "service_id": str(seed.consulta),
                    "date_from": WED.isoformat(),
                    "date_to": WED.isoformat(),
                    "preferred_periods": ["afternoon"],
                },
            ),
            text_response("Tenho quarta-feira, 11 de novembro, às 9h45. Serve?"),  # nao ofertado
            text_response("Tenho quarta-feira, 11 de novembro, às 13h30. Serve?"),  # ofertado
        ]
    )

    outcome = await engine_for(settings, provider).run_turn(turn_for(seed, config))

    assert outcome.regenerated
    assert outcome.violations
    assert "13h30" in outcome.reply
    assert "9h45" not in outcome.reply


@dataclass
class FlowModel:
    """Um modelo que faz o certo: le o resultado da tool anterior e usa o que veio nele."""

    service_id: str
    chosen_period: str = "afternoon"
    steps: list[str] = field(default_factory=list)

    async def complete(
        self,
        *,
        system: Sequence[PromptSegment],
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSchema] = (),
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        last = self._last_result(messages)
        if last is None:
            self.steps.append("check_availability")
            return tool_response(
                "check_availability",
                {
                    "service_id": self.service_id,
                    "date_from": WED.isoformat(),
                    "date_to": WED.isoformat(),
                    "preferred_periods": [self.chosen_period],
                },
            )
        if "slots" in last:
            self.steps.append("hold_slot")
            return tool_response("hold_slot", {"slot_token": last["slots"][0]["slot_token"]})
        if last.get("status") == "held":
            self.steps.append("confirm_appointment")
            return tool_response(
                "confirm_appointment",
                {"hold_id": last["hold_id"], "subject_name": "Mia", "intake": {"species": "gato"}},
            )
        self.steps.append("reply")
        return text_response(str(last["confirmation_message"]))

    @staticmethod
    def _last_result(messages: Sequence[LLMMessage]) -> dict[str, Any] | None:
        for message in reversed(messages):
            if isinstance(message.content, tuple):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        decoded = json.loads(block.content)
                        assert isinstance(decoded, dict)
                        return decoded
        return None


async def test_fluxo_completo_pelo_motor_do_check_ate_a_confirmacao(
    seed: BookingSeed, config: TenantConfig, settings: Settings, sync_engine: sa.Engine
) -> None:
    model = FlowModel(service_id=str(seed.consulta))

    outcome = await engine_for(settings, model).run_turn(turn_for(seed, config))

    assert model.steps == ["check_availability", "hold_slot", "confirm_appointment", "reply"]
    assert outcome.used_tools == ("check_availability", "hold_slot", "confirm_appointment")
    assert all(call.ok for call in outcome.tool_calls)
    assert outcome.stage == "booked"
    # A resposta final e o texto de confirmacao do tenant e passou pelo guardrail de saida
    # (hora e data vieram do resultado da propria confirmacao) sem ser descartada.
    assert outcome.violations == ()
    assert not outcome.regenerated
    assert "*Consulta* para *Mia*" in outcome.reply
    assert "13h30" in outcome.reply
    assert count(sync_engine, "appointments", seed.tenant) == 1
