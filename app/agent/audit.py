"""Trilha de auditoria das chamadas de tool (RF-30, secao 11.1 passo 7).

Toda chamada de tool vira uma linha em `audit_log` — inclusive a que falhou e a que foi
recusada por idempotencia. O log e o unico lugar onde da para responder, meses depois,
"por que o agente marcou isso": ele guarda os argumentos que o modelo produziu, o
resultado e quantas iteracoes o turno levou.

A escrita nunca derruba o turno. Um `audit_log` indisponivel e um problema de
observabilidade; deixar a pessoa sem resposta por causa dele seria trocar um problema
pequeno por um grande.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import sqlalchemy as sa

from app.core.config import Settings
from app.core.db import tenant_session
from app.core.telemetry import get_logger

log = get_logger(__name__)

__all__ = ["AuditEntry", "AuditSink", "NullAuditSink", "PostgresAuditSink"]


@dataclass(frozen=True, slots=True)
class AuditEntry:
    tenant_id: uuid.UUID
    actor: str
    action: str
    entity: str | None = None
    entity_id: uuid.UUID | None = None
    payload: dict[str, Any] | None = None


class AuditSink(Protocol):
    async def record(self, entry: AuditEntry) -> None: ...


class NullAuditSink:
    """Descarta. Usado em teste e no runner conversacional (S11)."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


@dataclass(slots=True)
class PostgresAuditSink:
    """Grava em `audit_log` dentro da sessao de tenant (RLS ligada)."""

    settings: Settings | None = None

    async def record(self, entry: AuditEntry) -> None:
        try:
            async with tenant_session(entry.tenant_id, self.settings) as session:
                await session.execute(
                    sa.text(
                        "INSERT INTO audit_log (tenant_id, actor, action, entity, entity_id,"
                        " payload) VALUES (:tenant_id, :actor, :action, :entity, :entity_id,"
                        " CAST(:payload AS jsonb))"
                    ),
                    {
                        "tenant_id": entry.tenant_id,
                        "actor": entry.actor,
                        "action": entry.action,
                        "entity": entry.entity,
                        "entity_id": entry.entity_id,
                        "payload": _json(entry.payload),
                    },
                )
        except Exception:
            log.exception("audit_log_falhou", action=entry.action)


def _json(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, default=str)
