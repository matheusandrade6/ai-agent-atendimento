"""Hold, advisory lock e confirmacao idempotente (secao 13.4; RF-19).

As tres camadas contra overbooking (invariante 6), na ordem em que uma reserva passa por
elas:

1. **Hold com TTL** — `hold_slot` grava a linha em `holds` na mesma transacao em que
   confere que nenhum agendamento ativo nem hold vigente se sobrepoe ao bloco (atendimento
   + buffers, D-44). A checagem e a do motor (`db_occupation`): o que o motor ofereceu e o
   que o hold aceita sao a mesma regra.
2. **Advisory lock** — `pg_advisory_xact_lock` por (profissional, dia) e por (recurso,
   dia), tomado antes da checagem, no hold e na confirmacao. Serializa quem disputa o
   mesmo profissional no mesmo dia; quem disputa outro nao espera.
3. **Constraint `appt_no_overlap`** — a rede final. Se mesmo assim duas confirmacoes
   chegarem ao `INSERT`, o banco recusa a segunda e ela vira `{"status": "slot_taken"}`.

Idempotencia: `confirm_appointment` grava `idempotency_key = sha256(hold_id)`. Repetir a
confirmacao do mesmo hold devolve o mesmo `appointment_id` — o hold e travado com
`FOR UPDATE` antes da busca pela chave, entao duas confirmacoes simultaneas do mesmo hold
tambem resolvem para uma linha so.

Quem chama abre a transacao (`tenant_session`) e faz o commit; os locks sao de transacao e
somem com ela. O evento no calendario **nao** e criado aqui: depois do commit, quem chama
usa `app.calendar.sync_state.push_appointment_event` (D-43: nunca segurar o lock durante a
retentativa de uma API de terceiros). Ate la o agendamento fica `pending_sync`.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar.base import Interval
from app.domain.models import (
    Appointment,
    Conversation,
    Hold,
    Provider,
    Resource,
    Service,
    ServiceProvider,
    ServiceResource,
)
from app.scheduling.availability import db_occupation
from app.scheduling.rules import ServiceDuration, fits_capacity, overlaps

__all__ = [
    "ConfirmResult",
    "HoldResult",
    "confirm_appointment",
    "expire_holds",
    "hold_slot",
    "idempotency_key_for",
    "lock_keys",
]

#: Constraint de exclusao da migration 0002 e o SQLSTATE `exclusion_violation`.
OVERLAP_CONSTRAINT = "appt_no_overlap"
_EXCLUSION_VIOLATION = "23P01"
IDEMPOTENCY_CONSTRAINT = "uq_appointment_idempotency"

#: Folga na leitura da ocupacao, pela mesma razao do motor: um atendimento longo, com
#: buffers, que comeca bem antes do bloco ainda pode alcanca-lo.
_OCCUPATION_MARGIN = timedelta(days=1)

HoldStatus = Literal["held", "slot_taken", "invalid"]
ConfirmStatus = Literal["confirmed", "slot_taken", "hold_expired", "hold_not_found"]


@dataclass(frozen=True, slots=True)
class HoldResult:
    status: HoldStatus
    hold_id: uuid.UUID | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    expires_at: datetime | None = None
    #: So em `invalid`: o que estava errado no pedido. Nao e texto para o cliente.
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status}
        if self.hold_id is not None:
            payload["hold_id"] = str(self.hold_id)
        for name in ("starts_at", "ends_at", "expires_at"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value.isoformat()
        if self.reason:
            payload["reason"] = self.reason
        return payload


@dataclass(frozen=True, slots=True)
class ConfirmResult:
    status: ConfirmStatus
    appointment_id: uuid.UUID | None = None
    #: `True` quando a confirmacao ja tinha acontecido: nada foi criado agora.
    replayed: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status}
        if self.appointment_id is not None:
            payload["appointment_id"] = str(self.appointment_id)
        return payload


# ------------------------------------ chaves ------------------------------------


def idempotency_key_for(hold_id: uuid.UUID) -> str:
    """`hash(hold_id)` da 13.4. Estavel entre processos e versoes do Python."""
    return hashlib.sha256(f"hold:{hold_id}".encode()).hexdigest()


def _days_touched(block: Interval) -> list[date]:
    first = block.start.astimezone(UTC).date()
    last = (block.end.astimezone(UTC) - timedelta(microseconds=1)).date()
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def lock_keys(owners: Iterable[tuple[str, uuid.UUID]], block: Interval) -> list[str]:
    """Chaves do advisory lock: uma por (dono, dia UTC) que o bloco toca.

    O dia e so particao do lock, nao regra de negocio — por isso UTC, e nao a timezone
    da unidade. Travar **todos** os dias que o bloco toca garante que dois blocos que se
    sobrepoem compartilham pelo menos uma chave, inclusive atravessando a meia-noite.
    """
    days = _days_touched(block)
    return sorted({f"{kind}:{owner}:{day.isoformat()}" for kind, owner in owners for day in days})


async def _acquire(session: AsyncSession, keys: list[str]) -> None:
    """Trava as chaves numa instrucao so, em ordem de hash.

    Dois pedidos que precisam de chaves em comum as pegam na mesma ordem, entao nao ha
    deadlock entre eles — nem quando `hashtext` colide para chaves diferentes.
    """
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock(h) FROM ("
            "  SELECT DISTINCT hashtext(k) AS h FROM unnest(CAST(:keys AS text[])) AS k"
            "  ORDER BY h"
            ") AS ordered"
        ),
        {"keys": keys},
    )


def _owners(provider_id: uuid.UUID, resource_id: uuid.UUID | None) -> list[tuple[str, uuid.UUID]]:
    owners = [("provider", provider_id)]
    if resource_id is not None:
        owners.append(("resource", resource_id))
    return owners


async def _conflicts(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    block: Interval,
    provider_id: uuid.UUID,
    resource: Resource | None,
    now: datetime,
    exclude_hold_id: uuid.UUID | None = None,
) -> bool:
    """O bloco esbarra em agendamento ativo ou hold vigente? Mesma regra do motor."""
    by_provider, by_resource = await db_occupation(
        session,
        tenant_id,
        provider_ids=[provider_id],
        resource_ids=[resource.id] if resource else [],
        start=block.start - _OCCUPATION_MARGIN,
        end=block.end + _OCCUPATION_MARGIN,
        now=now,
        exclude_hold_id=exclude_hold_id,
    )
    if any(overlaps(block, busy) for busy in by_provider.get(provider_id, [])):
        return True
    return resource is not None and not fits_capacity(
        block, by_resource.get(resource.id, []), resource.capacity
    )


def _require_aware(**moments: datetime) -> None:
    for name, moment in moments.items():
        if moment.tzinfo is None:
            raise ValueError(f"`{name}` ingenuo: todo instante precisa de timezone")


# ------------------------------------- hold -------------------------------------


async def hold_slot(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    provider_id: uuid.UUID,
    service_id: uuid.UUID,
    starts_at: datetime,
    now: datetime,
    ttl: timedelta,
    resource_id: uuid.UUID | None = None,
) -> HoldResult:
    """Reserva o slot por `ttl` (camada 1), sob o advisory lock (camada 2).

    O fim do atendimento vem do banco (`duration_override_min` ou a duracao do servico),
    nunca de quem chama: o modelo escolhe o horario entre os oferecidos, nao a duracao.
    A checagem nao consulta o calendario externo — isso ja aconteceu na oferta, e rede
    dentro do lock seguraria o profissional durante a retentativa (D-43).
    """
    _require_aware(starts_at=starts_at, now=now)
    if starts_at <= now:
        return HoldResult(status="invalid", reason="horario no passado")

    conversation = await session.scalar(
        select(Conversation.id).where(
            Conversation.tenant_id == tenant_id, Conversation.id == conversation_id
        )
    )
    if conversation is None:
        return HoldResult(status="invalid", reason="conversa inexistente")
    service = await session.scalar(
        select(Service).where(
            Service.tenant_id == tenant_id, Service.id == service_id, Service.active.is_(True)
        )
    )
    if service is None:
        return HoldResult(status="invalid", reason="servico inexistente ou inativo")
    link = (
        await session.execute(
            select(ServiceProvider.duration_override_min)
            .join(Provider, Provider.id == ServiceProvider.provider_id)
            .where(
                Provider.tenant_id == tenant_id,
                Provider.id == provider_id,
                Provider.active.is_(True),
                ServiceProvider.service_id == service.id,
            )
        )
    ).one_or_none()
    if link is None:
        return HoldResult(status="invalid", reason="profissional nao atende o servico")

    linked_resources = set(
        await session.scalars(
            select(ServiceResource.resource_id).where(ServiceResource.service_id == service.id)
        )
    )
    resource: Resource | None = None
    if linked_resources:
        if resource_id not in linked_resources:
            return HoldResult(status="invalid", reason="servico exige um recurso apto")
        resource = await session.scalar(
            select(Resource).where(Resource.tenant_id == tenant_id, Resource.id == resource_id)
        )
        if resource is None:
            return HoldResult(status="invalid", reason="recurso inexistente")
    elif resource_id is not None:
        return HoldResult(status="invalid", reason="servico nao usa recurso")

    duration = ServiceDuration.resolve(
        duration_minutes=service.duration_minutes,
        buffer_before_min=service.buffer_before_min,
        buffer_after_min=service.buffer_after_min,
        override_minutes=link.duration_override_min,
    )
    block = duration.block_for(starts_at)
    ends_at = starts_at + duration.core

    await _acquire(session, lock_keys(_owners(provider_id, resource_id), block))
    if await _conflicts(
        session, tenant_id, block=block, provider_id=provider_id, resource=resource, now=now
    ):
        return HoldResult(status="slot_taken")

    expires_at = now + ttl
    hold_id = await session.scalar(
        insert(Hold)
        .values(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            provider_id=provider_id,
            resource_id=resource_id,
            service_id=service.id,
            starts_at=starts_at,
            ends_at=ends_at,
            expires_at=expires_at,
        )
        .returning(Hold.id)
    )
    assert hold_id is not None
    return HoldResult(
        status="held", hold_id=hold_id, starts_at=starts_at, ends_at=ends_at, expires_at=expires_at
    )


# ---------------------------------- confirmacao ----------------------------------


def _violated_constraint(exc: IntegrityError) -> str | None:
    """Nome da constraint violada, atravessando o adaptador do asyncpg."""
    for error in (exc.orig, getattr(exc.orig, "__cause__", None)):
        name = getattr(error, "constraint_name", None)
        if name:
            return str(name)
    message = str(exc.orig)
    for name in (OVERLAP_CONSTRAINT, IDEMPOTENCY_CONSTRAINT):
        if name in message:
            return name
    return None


def _is_overlap_violation(exc: IntegrityError) -> bool:
    if _violated_constraint(exc) == OVERLAP_CONSTRAINT:
        return True
    for error in (exc.orig, getattr(exc.orig, "__cause__", None)):
        if getattr(error, "sqlstate", None) == _EXCLUSION_VIOLATION:
            return True
    return False


async def _existing(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> uuid.UUID | None:
    found: uuid.UUID | None = await session.scalar(
        select(Appointment.id).where(
            Appointment.tenant_id == tenant_id, Appointment.idempotency_key == key
        )
    )
    return found


async def _set_hold_status(session: AsyncSession, hold_id: uuid.UUID, status: str) -> None:
    await session.execute(update(Hold).where(Hold.id == hold_id).values(status=status))


async def confirm_appointment(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    hold_id: uuid.UUID,
    now: datetime,
    subject_id: uuid.UUID | None = None,
    intake: Mapping[str, Any] | None = None,
) -> ConfirmResult:
    """Transforma o hold em agendamento, uma vez so.

    Ordem importa: (1) trava o hold com `FOR UPDATE`; (2) procura agendamento ja criado
    com a chave do hold — repeticao devolve o mesmo id; (3) confere validade do hold;
    (4) advisory lock e checagem de novo, sem contar o proprio hold; (5) `INSERT` num
    savepoint, onde a `appt_no_overlap` e a ultima palavra.
    """
    _require_aware(now=now)
    hold = (
        await session.execute(
            select(Hold).where(Hold.tenant_id == tenant_id, Hold.id == hold_id).with_for_update()
        )
    ).scalar_one_or_none()
    if hold is None:
        return ConfirmResult(status="hold_not_found")

    key = idempotency_key_for(hold.id)
    existing = await _existing(session, tenant_id, key)
    if existing is not None:
        return ConfirmResult(status="confirmed", appointment_id=existing, replayed=True)

    if hold.status != "active":
        return ConfirmResult(status="hold_expired")
    if hold.expires_at <= now:
        await _set_hold_status(session, hold.id, "expired")
        return ConfirmResult(status="hold_expired")

    service = await session.scalar(
        select(Service).where(Service.tenant_id == tenant_id, Service.id == hold.service_id)
    )
    assert service is not None  # FK; RLS do mesmo tenant
    resource: Resource | None = None
    if hold.resource_id is not None:
        resource = await session.scalar(
            select(Resource).where(Resource.tenant_id == tenant_id, Resource.id == hold.resource_id)
        )
    block = Interval(
        hold.starts_at - timedelta(minutes=service.buffer_before_min),
        hold.ends_at + timedelta(minutes=service.buffer_after_min),
    )

    await _acquire(session, lock_keys(_owners(hold.provider_id, hold.resource_id), block))
    if await _conflicts(
        session,
        tenant_id,
        block=block,
        provider_id=hold.provider_id,
        resource=resource,
        now=now,
        exclude_hold_id=hold.id,
    ):
        await _set_hold_status(session, hold.id, "expired")
        return ConfirmResult(status="slot_taken")

    contact_id = await session.scalar(
        select(Conversation.contact_id).where(
            Conversation.tenant_id == tenant_id, Conversation.id == hold.conversation_id
        )
    )
    location_id = await session.scalar(
        select(Provider.location_id).where(
            Provider.tenant_id == tenant_id, Provider.id == hold.provider_id
        )
    )
    try:
        async with session.begin_nested():
            appointment_id = await session.scalar(
                insert(Appointment)
                .values(
                    tenant_id=tenant_id,
                    contact_id=contact_id,
                    subject_id=subject_id,
                    conversation_id=hold.conversation_id,
                    provider_id=hold.provider_id,
                    resource_id=hold.resource_id,
                    service_id=hold.service_id,
                    location_id=location_id,
                    starts_at=hold.starts_at,
                    ends_at=hold.ends_at,
                    status="confirmed",
                    source="agent",
                    # O evento ainda nao existe: quem chama faz o push depois do commit.
                    # Se o processo morrer antes, a linha ja diz que falta sincronizar.
                    sync_status="pending_sync",
                    idempotency_key=key,
                    intake=dict(intake or {}),
                )
                .returning(Appointment.id)
            )
    except IntegrityError as exc:
        if _is_overlap_violation(exc):
            await _set_hold_status(session, hold.id, "expired")
            return ConfirmResult(status="slot_taken")
        if _violated_constraint(exc) == IDEMPOTENCY_CONSTRAINT:
            existing = await _existing(session, tenant_id, key)
            if existing is not None:
                return ConfirmResult(status="confirmed", appointment_id=existing, replayed=True)
        raise
    assert appointment_id is not None
    await _set_hold_status(session, hold.id, "consumed")
    return ConfirmResult(status="confirmed", appointment_id=appointment_id)


# ----------------------------------- expiracao -----------------------------------


async def expire_holds(session: AsyncSession, now: datetime) -> int:
    """Marca como `expired` todo hold ativo vencido. Devolve quantos.

    Limpeza, nao correcao: toda leitura de ocupacao ja ignora hold com `expires_at <= now`.
    O job existe para o indice parcial (`status = 'active'`) nao crescer sem fim e para o
    painel nao mostrar reserva morta como viva. Atravessa tenants: `bypass_rls_session`.
    """
    _require_aware(now=now)
    result = await session.execute(
        update(Hold)
        .where(Hold.status == "active", Hold.expires_at <= now)
        .values(status="expired")
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)
