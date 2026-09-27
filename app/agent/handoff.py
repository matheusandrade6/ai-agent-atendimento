"""Servico de handoff: abre, notifica e retoma por timeout (RF-26..RF-30, secao 12.5).

Uma porta so de entrada
------------------------
Handoff pode nascer de tres lugares — um gatilho de entrada (`app.agent.guardrails`), a
tool `escalate_to_human` (`app.agent.tools.escalate_to_human`) ou o disjuntor/teto de
iteracoes do motor (`app.agent.engine`) — mas so um lugar abre a linha em `handoffs`,
silencia a conversa e notifica: este arquivo, chamado por `app.agent.runner` depois que
o `TurnOutcome.escalate` do turno ja esta decidido. Sem essa porta unica, cada origem
reinventaria a idempotencia da notificacao (RF-27, "enviada uma unica vez") e a regra de
silencio (RF-28) do jeito dela.

Silencio e retomada (RF-28)
----------------------------
`conversations.silenced_until` e o unico relogio: `HandoffService.should_run_turn` decide,
antes de qualquer chamada ao modelo, se o turno roda. Retomada manual (painel, S17) muda
`status` e `silenced_until` direto no banco e este servico nunca precisa saber disso — ele
so cobre a retomada automatica por timeout.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

import sqlalchemy as sa

from app.agent.audit import AuditEntry, AuditSink, NullAuditSink
from app.channels.base import ChannelError, OutboundChannel
from app.core.config import Settings
from app.core.db import tenant_session
from app.core.telemetry import get_logger
from app.domain.tenant_config import NotifyTarget, TenantConfig

log = get_logger(__name__)

__all__ = [
    "HandoffNotifier",
    "HandoffOpened",
    "HandoffService",
    "NullHandoffNotifier",
    "WhatsAppHandoffNotifier",
    "triggered_by_for",
]


@dataclass(frozen=True, slots=True)
class HandoffOpened:
    handoff_id: uuid.UUID
    #: `False` quando ja havia handoff aberto para a conversa — a chamada foi ignorada
    #: de proposito (idempotencia: nunca dois handoffs abertos ao mesmo tempo).
    opened: bool
    notified: bool


class HandoffNotifier(Protocol):
    async def notify(
        self,
        *,
        target: NotifyTarget,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        reason: str,
        summary: str,
    ) -> bool:
        """Tenta notificar um destino. Devolve `True` se o envio saiu."""
        ...


class NullHandoffNotifier:
    """Nao envia nada. Usado em teste e como notificador padrao sem canal configurado."""

    def __init__(self) -> None:
        self.calls: list[tuple[NotifyTarget, str, str]] = []

    async def notify(
        self,
        *,
        target: NotifyTarget,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        reason: str,
        summary: str,
    ) -> bool:
        self.calls.append((target, reason, summary))
        return False


@dataclass(slots=True)
class WhatsAppHandoffNotifier:
    """Notifica por WhatsApp usando o canal de saida do tenant (14.1.3).

    So `channel == "whatsapp"` tem transporte hoje. `email` e `panel` entram com o
    provedor de email e o painel (S17), que ainda nao existem — sem eles, so registramos
    e seguimos: o caminho critico do RF-27 (avisar o responsavel) ja fica coberto pelo
    WhatsApp, que e o canal que toda config de exemplo usa em `escalation.notify`.
    """

    channel: OutboundChannel

    async def notify(
        self,
        *,
        target: NotifyTarget,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        reason: str,
        summary: str,
    ) -> bool:
        if target.channel != "whatsapp":
            log.info("handoff_notificacao_canal_nao_suportado", canal=target.channel)
            return False
        try:
            await self.channel.send_text(to=target.to, text=_notification_text(reason, summary))
        except ChannelError:
            log.exception("handoff_notificacao_falhou", destino=target.to)
            return False
        return True


def _notification_text(reason: str, summary: str) -> str:
    return f"Atendimento precisa de humano.\nMotivo: {reason}\nResumo: {summary}"


def triggered_by_for(reason: str | None) -> str:
    """Deriva `handoffs.triggered_by` do motivo do turno (`CheckConstraint handoff_source`).

    `tool:` e o modelo decidindo por conta propria (`escalate_to_human`); `trigger:` e um
    gatilho de entrada casando com o que a pessoa escreveu — o disparo e do sistema, mas a
    origem e o que o contato disse, por isso 'contact'; o resto (disjuntor de custo/
    mensagens, teto de iteracoes, segunda falha do guardrail de saida) e o processo do
    agente tropecando na propria regra, sem um gatilho de config por tras.
    """
    if reason is None:
        return "rule"
    if reason.startswith("tool:"):
        return "agent"
    if reason.startswith("trigger:"):
        return "contact"
    if reason in ("max_tool_iterations", "output_guardrail"):
        return "agent"
    return "rule"


@dataclass(slots=True)
class HandoffService:
    audit: AuditSink = field(default_factory=NullAuditSink)
    settings: Settings | None = None

    async def should_run_turn(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        status: str,
        silenced_until: datetime | None,
        now: datetime,
    ) -> bool:
        """Passo 0 do turno (RF-28): decide se o motor roda.

        Fecha o handoff por timeout e devolve `True` quando o prazo ja passou — o turno
        que chega logo depois do silencio e o que retoma a conversa, nao um job a parte.
        Sem `silenced_until` gravado, so a retomada manual resolve: fica em silencio.
        """
        if status != "handoff":
            return True
        if silenced_until is None:
            return False
        if silenced_until > now:
            return False
        await self.resume_by_timeout(tenant_id=tenant_id, conversation_id=conversation_id, now=now)
        return True

    async def resume_by_timeout(
        self, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID, now: datetime
    ) -> bool:
        """Fecha o handoff aberto e volta a conversa para `active`. Devolve se algo mudou."""
        async with tenant_session(tenant_id, self.settings) as session:
            handoff_id = (
                await session.execute(
                    sa.text(
                        "UPDATE handoffs SET closed_at = :now WHERE conversation_id = :cid"
                        " AND closed_at IS NULL RETURNING id"
                    ),
                    {"now": now, "cid": conversation_id},
                )
            ).scalar_one_or_none()
            await session.execute(
                sa.text(
                    "UPDATE conversations SET status = 'active', silenced_until = NULL"
                    " WHERE id = :id"
                ),
                {"id": conversation_id},
            )

        log.info("handoff_retomado_por_timeout", handoff_id=str(handoff_id) if handoff_id else None)
        if handoff_id is not None:
            await self.audit.record(
                AuditEntry(
                    tenant_id=tenant_id,
                    actor="system",
                    action="handoff.resumed_timeout",
                    entity="handoff",
                    entity_id=handoff_id,
                    payload={"conversation_id": str(conversation_id)},
                )
            )
        return True

    async def open(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        config: TenantConfig,
        reason: str,
        triggered_by: str,
        summary: str,
        now: datetime,
        notifier: HandoffNotifier | None = None,
    ) -> HandoffOpened:
        """Abre o handoff se nao houver um aberto, silencia a conversa e notifica (RF-26..28).

        Idempotente por conversa: uma segunda chamada enquanto o handoff anterior segue
        aberto nao cria outra linha nem manda outra notificacao (RF-27, "enviada uma unica
        vez") — ela so devolve o handoff que ja existia.
        """
        async with tenant_session(tenant_id, self.settings) as session:
            existing = (
                await session.execute(
                    sa.text(
                        "SELECT id FROM handoffs WHERE conversation_id = :cid"
                        " AND closed_at IS NULL LIMIT 1"
                    ),
                    {"cid": conversation_id},
                )
            ).first()
            if existing is not None:
                log.info("handoff_ja_aberto", handoff_id=str(existing.id))
                return HandoffOpened(handoff_id=existing.id, opened=False, notified=False)

            silenced_until = now + timedelta(minutes=config.escalation.handoff_silence_minutes)
            # A anotacao nao e enfeite: no SQLAlchemy 2.1 o `scalar_one()` de um
            # `TextClause` devolve um type var que o mypy nao consegue resolver, e
            # `mypy --strict` para em "Need type annotation". Com 2.0 passava — foi assim
            # que isto chegou verde na S10 e vermelho no CI, que instala a versao nova.
            handoff_id: uuid.UUID = (
                await session.execute(
                    sa.text(
                        "INSERT INTO handoffs (tenant_id, conversation_id, reason,"
                        " triggered_by, summary) VALUES (:tenant_id, :conversation_id,"
                        " :reason, :triggered_by, :summary) RETURNING id"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "conversation_id": conversation_id,
                        "reason": reason,
                        "triggered_by": triggered_by,
                        "summary": summary,
                    },
                )
            ).scalar_one()
            await session.execute(
                sa.text(
                    "UPDATE conversations SET status = 'handoff', silenced_until = :until"
                    " WHERE id = :id"
                ),
                {"until": silenced_until, "id": conversation_id},
            )

        log.info("handoff_aberto", handoff_id=str(handoff_id), motivo=reason, por=triggered_by)
        await self.audit.record(
            AuditEntry(
                tenant_id=tenant_id,
                actor="system",
                action="handoff.opened",
                entity="handoff",
                entity_id=handoff_id,
                payload={
                    "reason": reason,
                    "triggered_by": triggered_by,
                    "conversation_id": str(conversation_id),
                },
            )
        )

        notified = await self._notify(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            handoff_id=handoff_id,
            config=config,
            reason=reason,
            summary=summary,
            notifier=notifier,
        )
        return HandoffOpened(handoff_id=handoff_id, opened=True, notified=notified)

    async def _notify(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        handoff_id: uuid.UUID,
        config: TenantConfig,
        reason: str,
        summary: str,
        notifier: HandoffNotifier | None,
    ) -> bool:
        if notifier is None or not config.escalation.notify:
            return False

        sent_any = False
        for target in config.escalation.notify:
            try:
                sent = await notifier.notify(
                    target=target,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    reason=reason,
                    summary=summary,
                )
            except Exception:
                log.exception("handoff_notificador_excecao", destino=target.to)
                sent = False
            sent_any = sent_any or sent

        if not sent_any:
            return False

        async with tenant_session(tenant_id, self.settings) as session:
            await session.execute(
                sa.text("UPDATE handoffs SET notified_at = now() WHERE id = :id"),
                {"id": handoff_id},
            )
        await self.audit.record(
            AuditEntry(
                tenant_id=tenant_id,
                actor="system",
                action="handoff.notified",
                entity="handoff",
                entity_id=handoff_id,
                payload={"reason": reason},
            )
        )
        return True
