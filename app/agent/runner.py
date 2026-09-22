"""Ponte entre a rajada agregada e o motor (passos 1, 2, 9 e 10 da secao 11.1).

O motor (`app.agent.engine`) e puro em relacao a banco e fila: recebe estado, devolve
resultado. Este modulo e quem faz o resto — carrega o estado da conversa, chama o motor,
persiste mensagem, custo, `collected`, `stage` e `summary`, e enfileira o envio.

Separar assim tem uma consequencia pratica: o teste do loop nao precisa de Postgres, e o
teste de persistencia nao precisa de LLM.

`turn_handler` do worker
------------------------
`install(ctx, engine)` instala este handler no `ctx` do arq, na chave que a S05 deixou
reservada. Sem instalacao, o worker segue com o handler que so registra a rajada.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from app.agent.engine import AgentEngine, ConversationState, TurnOutcome, TurnRequest
from app.agent.memory import StoredMessage, should_summarize
from app.agent.prompt import PersonContext
from app.core.config import Settings
from app.core.db import tenant_session
from app.core.telemetry import get_logger
from app.core.time import now_in
from app.domain.tenant_registry import load_config_by_tenant_id
from app.workers.aggregator import AggregatedTurn
from app.workers.base import WorkerContext, ctx_outbound, ctx_settings
from app.workers.queue import OutboundMessage

log = get_logger(__name__)

__all__ = ["AgentTurnHandler", "install", "load_state", "persist_turn"]

#: Quantas mensagens da conversa a janela precisa ver. Um pouco alem de `WINDOW_SIZE`
#: para o resumo rolante ter material do que ficou de fora.
_HISTORY_LIMIT = 60


@dataclass(frozen=True, slots=True)
class LoadedConversation:
    state: ConversationState
    contact_id: uuid.UUID
    channel: str
    contact_name: str | None


async def load_state(
    tenant_id: uuid.UUID, conversation_id: uuid.UUID, settings: Settings | None = None
) -> LoadedConversation | None:
    """Le a conversa e o historico dentro da sessao de tenant (RLS ligada).

    A ordem sai de `created_at`, que a migration 0005 amarrou a `clock_timestamp()`.
    Nao ha criterio de desempate possivel — a PK e UUID aleatorio —, entao a ordem da
    conversa depende de o instante de cada linha ser realmente distinto.
    """
    async with tenant_session(tenant_id, settings) as session:
        row = (
            await session.execute(
                sa.text(
                    "SELECT c.contact_id, c.channel, c.stage, c.collected, c.summary,"
                    " c.summary_message_count, ct.name AS contact_name"
                    " FROM conversations c JOIN contacts ct ON ct.id = c.contact_id"
                    " WHERE c.id = :id"
                ),
                {"id": conversation_id},
            )
        ).first()
        if row is None:
            return None

        history_rows = (
            await session.execute(
                sa.text(
                    "SELECT direction, author, content, created_at FROM messages"
                    " WHERE conversation_id = :id AND content IS NOT NULL"
                    " ORDER BY created_at DESC LIMIT :limit"
                ),
                {"id": conversation_id, "limit": _HISTORY_LIMIT},
            )
        ).all()
        totals = (
            await session.execute(
                sa.text(
                    "SELECT COUNT(*) AS total, COALESCE(SUM(cost_usd), 0) AS cost"
                    " FROM messages WHERE conversation_id = :id"
                ),
                {"id": conversation_id},
            )
        ).one()

    history = tuple(
        StoredMessage(
            direction=r.direction, author=r.author, content=r.content or "", created_at=r.created_at
        )
        for r in reversed(history_rows)
    )
    return LoadedConversation(
        state=ConversationState(
            history=history,
            summary=row.summary,
            collected=dict(row.collected or {}),
            stage=row.stage,
            message_count=int(totals.total),
            summarized_message_count=int(row.summary_message_count or 0),
            cost_so_far_usd=Decimal(str(totals.cost or 0)),
        ),
        contact_id=row.contact_id,
        channel=row.channel,
        contact_name=row.contact_name,
    )


async def persist_turn(
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    outcome: TurnOutcome,
    *,
    message_count: int,
    settings: Settings | None = None,
) -> list[uuid.UUID]:
    """Grava as mensagens do turno com tokens e custo, e atualiza a conversa (passo 9).

    Tudo numa transacao so: uma resposta gravada com o `stage` antigo, ou um `collected`
    atualizado sem a mensagem que o gerou, deixam a proxima conversa mentindo.

    Uma linha por mensagem enviada, porque o guardrail de saida pode ter quebrado a
    resposta em varias (11.5) e o painel precisa mostrar o que a pessoa realmente
    recebeu. **Tokens e custo vao so na primeira linha**: eles sao do turno, nao da
    mensagem, e repeti-los multiplicaria o custo da conversa — que e exatamente o numero
    que o disjuntor le para decidir escalar.
    """
    parts = _messages_of(outcome)
    async with tenant_session(tenant_id, settings) as session:
        message_ids: list[uuid.UUID] = []
        for index, text in enumerate(parts):
            first = index == 0
            message_ids.append(
                (
                    await session.execute(
                        sa.text(
                            "INSERT INTO messages (tenant_id, conversation_id, direction,"
                            " author, content_type, content, tool_calls, tokens_in, tokens_out,"
                            " cost_usd) VALUES (:tenant_id, :conversation_id, 'outbound',"
                            " 'agent', 'text', :content, CAST(:tool_calls AS jsonb), :tokens_in,"
                            " :tokens_out, :cost_usd) RETURNING id"
                        ),
                        {
                            "tenant_id": tenant_id,
                            "conversation_id": conversation_id,
                            "content": text,
                            "tool_calls": _tool_calls_json(outcome) if first else _json({}),
                            "tokens_in": outcome.usage.total_input_tokens if first else 0,
                            "tokens_out": outcome.usage.output_tokens if first else 0,
                            "cost_usd": outcome.cost_usd if first else Decimal(0),
                        },
                    )
                ).scalar_one()
            )

        params: dict[str, Any] = {
            "id": conversation_id,
            "stage": outcome.stage,
            "collected": _json(dict(outcome.collected)),
            "status": "handoff" if outcome.escalate else "active",
        }
        sets = [
            "stage = :stage",
            "collected = CAST(:collected AS jsonb)",
            "last_message_at = now()",
        ]
        if outcome.escalate:
            sets.append("status = :status")
        if outcome.summary is not None:
            sets.append("summary = :summary")
            sets.append("summary_message_count = :summary_message_count")
            params["summary"] = outcome.summary
            params["summary_message_count"] = message_count
        await session.execute(
            sa.text(f"UPDATE conversations SET {', '.join(sets)} WHERE id = :id"), params
        )
    return message_ids


def _messages_of(outcome: TurnOutcome) -> tuple[str, ...]:
    """As partes a enviar. `messages` e a fonte; `reply` cobre quem construiu o outcome
    a mao (teste, painel) sem passar pelo guardrail de saida."""
    parts = tuple(text for text in outcome.messages if text.strip())
    if parts:
        return parts
    return (outcome.reply.strip(),) if outcome.reply.strip() else ()


@dataclass(slots=True)
class AgentTurnHandler:
    """Handler de turno instalado no worker de entrada (S05)."""

    engine: AgentEngine

    async def __call__(self, ctx: WorkerContext, turn: AggregatedTurn) -> None:
        settings = ctx_settings(ctx)
        config = await load_config_by_tenant_id(turn.tenant_id, settings)
        if config is None:
            log.warning("turno_sem_config_de_tenant")
            return

        loaded = await load_state(turn.tenant_id, turn.conversation_id, settings)
        if loaded is None:
            log.warning("turno_sem_conversa")
            return

        outcome = await self.engine.run_turn(
            TurnRequest(
                tenant_id=turn.tenant_id,
                conversation_id=turn.conversation_id,
                contact_id=loaded.contact_id,
                channel=loaded.channel,
                config=config,
                now=now_in(config.business_hours.timezone),
                state=loaded.state,
                person=_person_of(loaded),
            )
        )

        texts = _messages_of(outcome)
        message_ids = await persist_turn(
            turn.tenant_id,
            turn.conversation_id,
            outcome,
            message_count=loaded.state.message_count + len(texts),
            settings=settings,
        )
        log.info(
            "turno_concluido",
            stage=outcome.stage,
            iteracoes=outcome.iterations,
            tools=list(outcome.used_tools),
            tokens_in=outcome.usage.total_input_tokens,
            tokens_out=outcome.usage.output_tokens,
            custo_usd=str(outcome.cost_usd),
            escalonou=outcome.escalate,
            partes=len(texts),
            violacoes=list(outcome.violations),
            regenerou=outcome.regenerated,
        )

        outbound = ctx_outbound(ctx)
        delay = settings.outbound_part_delay_seconds
        for index, (message_id, text) in enumerate(zip(message_ids, texts, strict=True)):
            await outbound.enqueue_outbound(
                OutboundMessage(
                    tenant_id=turn.tenant_id,
                    conversation_id=turn.conversation_id,
                    text=text,
                    # A chave e da mensagem gravada: reentrega do job nao manda duas
                    # vezes o mesmo texto (invariante 5, ver `app.workers.outbound`).
                    idempotency_key=str(message_id),
                ),
                # A parte 2 espera a 1: a fila nao promete ordem entre jobs publicados
                # no mesmo instante, e uma resposta quebrada fora de ordem confunde.
                defer_seconds=index * delay,
            )


def install(ctx: WorkerContext, engine: AgentEngine) -> None:
    ctx["turn_handler"] = AgentTurnHandler(engine)


def _person_of(loaded: LoadedConversation) -> PersonContext:
    """Contexto de pessoa com o que o banco ja tem. Subjects e historico chegam na S08."""
    return PersonContext(contact_name=loaded.contact_name)


def _tool_calls_json(outcome: TurnOutcome) -> str:
    return _json(
        {
            "calls": [
                {
                    "name": call.name,
                    "kind": call.kind,
                    "ok": call.ok,
                    "iteration": call.iteration,
                    "duration_ms": call.duration_ms,
                    "deduplicated": call.deduplicated,
                }
                for call in outcome.tool_calls
            ],
            "iterations": outcome.iterations,
            "model": outcome.model,
        }
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def summarize_pending(state: ConversationState) -> bool:
    """Exposto para o teste e para o painel: este turno vai regenerar o resumo?"""
    return should_summarize(state.message_count, state.summarized_message_count)
