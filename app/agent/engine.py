"""Loop de tool calling do agente (secao 11.1).

O motor executa os passos 5 a 9 da 11.1. Os passos 1 e 2 sao do worker (S05) e do
carregador de estado (`app.agent.runner`); os guardrails (3 e 8) sao da S09 e entram
pelos ganchos `input_guardrail` / `output_guardrail`, que aqui tem padrao inerte.

As quatro regras duras deste arquivo
------------------------------------
1. **`tenant_id` vem do contexto, nunca dos argumentos** (invariante 3). Se o modelo
   inventar um `tenant_id` no JSON da tool, ele e descartado antes da execucao e o
   descarte fica registrado — nao e erro silencioso, e sinal de que algo tentou.
2. **Nenhuma tool de escrita roda duas vezes com a mesma chave de idempotencia no mesmo
   turno** (invariante 5 e regra do passo 7). A segunda chamada devolve o resultado da
   primeira. Isso importa porque o modelo *insiste*: e comum ele repetir
   `confirm_appointment` quando a primeira resposta nao veio no formato que esperava.
3. **`MAX_TOOL_ITERATIONS` e teto rigido.** Esgotado, o motor faz **uma** chamada final
   sem tools — o modelo precisa fechar com texto — e marca o turno para escalonamento.
   Sem esse fechamento, a pessoa ficaria sem resposta nenhuma.
4. **Custo e contado sempre**, inclusive das chamadas de extracao e de resumo. O
   disjuntor de custo por conversa (11.5) so vale se ninguem escapar da conta.

Conteudo do usuario continua sendo dado
---------------------------------------
O motor nunca concatena texto de cliente em instrucao. O que chega dele entra pela
janela de memoria, ja delimitado (`app.agent.memory`), e resultado de tool volta como
JSON dentro de `tool_result`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Protocol

from pydantic import ValidationError

from app.agent.audit import AuditEntry, AuditSink, NullAuditSink
from app.agent.llm import (
    LLMMessage,
    LLMProvider,
    LLMResponse,
    PromptSegment,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from app.agent.memory import (
    IntakeState,
    StoredMessage,
    build_window,
    extract_fields,
    intake_state,
    should_summarize,
    summarize,
)
from app.agent.prompt import (
    AgentPromptData,
    KnowledgeSnippet,
    PersonContext,
    ServiceSummary,
    build_system_prompt,
)
from app.agent.tools.base import ToolContext, ToolError, ToolRegistry, ToolResult, ToolSpec
from app.core.telemetry import get_logger, log_context
from app.domain.tenant_config import TenantConfig

log = get_logger(__name__)

__all__ = [
    "MAX_TOOL_ITERATIONS",
    "AgentEngine",
    "ConversationState",
    "ToolInvocation",
    "TurnOutcome",
    "TurnRequest",
    "next_stage",
]

#: Teto de idas e voltas com tools num unico turno (11.1, passo 7).
MAX_TOOL_ITERATIONS: Final[int] = 6

#: Chave que o modelo nao pode preencher. Ela existe no contexto, nunca nos argumentos.
_FORBIDDEN_ARGS: Final[frozenset[str]] = frozenset({"tenant_id"})


# --------------------------------- entrada ---------------------------------


@dataclass(frozen=True, slots=True)
class ConversationState:
    """Estado carregado do banco antes do turno (passo 2 da 11.1)."""

    history: Sequence[StoredMessage] = ()
    summary: str | None = None
    collected: Mapping[str, Any] = field(default_factory=dict)
    stage: str = "greeting"
    message_count: int = 0
    summarized_message_count: int = 0
    cost_so_far_usd: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class TurnRequest:
    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    contact_id: uuid.UUID
    channel: str
    config: TenantConfig
    now: datetime
    state: ConversationState = field(default_factory=ConversationState)
    services: Sequence[ServiceSummary] = ()
    knowledge: Sequence[KnowledgeSnippet] = ()
    person: PersonContext = field(default_factory=PersonContext)


# ---------------------------------- saida ----------------------------------


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Registro do que aconteceu numa chamada de tool, para log e para o painel."""

    name: str
    arguments: Mapping[str, Any]
    kind: str
    ok: bool
    iteration: int
    duration_ms: int
    deduplicated: bool = False
    result: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    reply: str
    stage: str
    collected: Mapping[str, Any]
    missing: tuple[str, ...]
    usage: Usage
    cost_usd: Decimal
    tool_calls: tuple[ToolInvocation, ...] = ()
    iterations: int = 0
    summary: str | None = None
    escalate: bool = False
    escalation_reason: str | None = None
    model: str = ""

    @property
    def used_tools(self) -> tuple[str, ...]:
        return tuple(call.name for call in self.tool_calls)


class Guardrail(Protocol):
    """Gancho da S09. O padrao nao mexe em nada."""

    async def __call__(self, request: TurnRequest, text: str) -> str: ...


# ---------------------------------- motor ----------------------------------


@dataclass(slots=True)
class AgentEngine:
    provider: LLMProvider
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    audit: AuditSink = field(default_factory=NullAuditSink)
    max_iterations: int = MAX_TOOL_ITERATIONS
    #: Extracao estruturada por turno (11.7). Desligavel para a suite conversacional,
    #: que compara respostas e nao quer uma segunda chamada no meio.
    extract: bool = True

    async def run_turn(self, request: TurnRequest) -> TurnOutcome:
        with log_context(
            tenant_id=str(request.tenant_id), conversation_id=str(request.conversation_id)
        ):
            return await self._run_turn(request)

    # ------------------------------ passos ------------------------------

    async def _run_turn(self, request: TurnRequest) -> TurnOutcome:
        breaker = self._circuit_breaker(request)
        if breaker is not None:
            return breaker

        usage = Usage()
        cost = Decimal(0)

        window = build_window(request.state.history, request.state.summary)

        collected: Mapping[str, Any] = request.state.collected
        if self.extract and request.config.intake.field_keys():
            extraction = await extract_fields(
                self.provider, request.config.intake, window, collected
            )
            collected = extraction.collected
            usage += extraction.usage
            cost += extraction.cost_usd

        state = intake_state(request.config.intake, collected)
        system = build_system_prompt(
            AgentPromptData(
                config=request.config,
                channel=request.channel,
                stage=request.state.stage,
                now=request.now,
                collected=state.collected,
                missing=state.missing,
                services=request.services,
                knowledge=request.knowledge,
                person=request.person,
            )
        )

        loop = await self._tool_loop(request, system=system, window=list(window), state=state)
        usage += loop.usage
        cost += loop.cost

        summary = await self._maybe_summarize(request)
        if summary is not None:
            usage += summary.usage
            cost += summary.cost

        stage = next_stage(
            request.state.stage,
            successful_tools=tuple(c.name for c in loop.calls if c.ok),
            intake_complete=state.is_complete,
            escalated=loop.escalate,
        )

        return TurnOutcome(
            reply=loop.reply,
            stage=stage,
            collected=state.collected,
            missing=state.missing,
            usage=usage,
            cost_usd=cost,
            tool_calls=tuple(loop.calls),
            iterations=loop.iterations,
            summary=summary.text if summary is not None else None,
            escalate=loop.escalate,
            escalation_reason=loop.escalation_reason,
            model=loop.model,
        )

    def _circuit_breaker(self, request: TurnRequest) -> TurnOutcome | None:
        """Disjuntor da 11.5: conversa longa ou cara demais vira handoff, sem chamar o LLM."""
        limits = request.config.limits
        state = request.state
        reason: str | None = None
        if state.message_count > limits.max_messages_per_conversation:
            reason = "max_messages_per_conversation"
        elif state.cost_so_far_usd > Decimal(str(limits.max_llm_cost_usd_per_conversation)):
            reason = "max_llm_cost_usd_per_conversation"
        if reason is None:
            return None

        log.warning("disjuntor_da_conversa", motivo=reason)
        return TurnOutcome(
            reply="",
            stage="handoff",
            collected=state.collected,
            missing=(),
            usage=Usage(),
            cost_usd=Decimal(0),
            escalate=True,
            escalation_reason=reason,
        )

    async def _tool_loop(
        self,
        request: TurnRequest,
        *,
        system: Sequence[PromptSegment],
        window: list[LLMMessage],
        state: IntakeState,
    ) -> _LoopResult:
        ctx = ToolContext(
            tenant_id=request.tenant_id,
            conversation_id=request.conversation_id,
            contact_id=request.contact_id,
            channel=request.channel,
            config=request.config,
            now=request.now,
            collected=state.collected,
        )
        schemas = self.tools.schemas()
        seen_keys: dict[str, ToolResult] = {}
        calls: list[ToolInvocation] = []
        usage = Usage()
        cost = Decimal(0)
        model = ""
        response: LLMResponse | None = None

        iterations = 0
        for iteration in range(1, self.max_iterations + 1):
            iterations = iteration
            response = await self.provider.complete(system=system, messages=window, tools=schemas)
            usage += response.usage
            cost += response.cost_usd
            model = response.model

            tool_calls = response.tool_calls
            if not tool_calls:
                return _LoopResult(
                    reply=response.text,
                    usage=usage,
                    cost=cost,
                    calls=calls,
                    iterations=iterations,
                    model=model,
                )

            window.append(response.assistant_message)
            results: list[ToolResultBlock] = []
            for block in tool_calls:
                invocation, result = await self._execute(ctx, block, iteration, seen_keys)
                calls.append(invocation)
                results.append(
                    ToolResultBlock(
                        tool_use_id=block.id,
                        content=result.as_json(),
                        is_error=result.is_error,
                    )
                )
            # Todos os `tool_result` da rodada numa unica mensagem de usuario: dividir
            # em varias ensina o modelo a parar de pedir tools em paralelo.
            window.append(LLMMessage(role="user", content=tuple(results)))

        # Teto estourado: uma ultima chamada sem tools, so para fechar com texto.
        log.warning("max_tool_iterations_atingido", iteracoes=iterations)
        closing = await self.provider.complete(system=system, messages=window)
        usage += closing.usage
        cost += closing.cost_usd
        return _LoopResult(
            reply=closing.text,
            usage=usage,
            cost=cost,
            calls=calls,
            iterations=iterations,
            model=closing.model or model,
            escalate=True,
            escalation_reason="max_tool_iterations",
        )

    async def _execute(
        self,
        ctx: ToolContext,
        block: ToolUseBlock,
        iteration: int,
        seen_keys: dict[str, ToolResult],
    ) -> tuple[ToolInvocation, ToolResult]:
        started = time.perf_counter()
        spec = self.tools.get(block.name)
        if spec is None:
            result = ToolResult(
                content={"error": "ferramenta_desconhecida", "tool": block.name}, is_error=True
            )
            invocation = self._invocation(block, "unknown", result, iteration, started)
            await self._audit(ctx, invocation)
            return invocation, result

        arguments = _strip_context_args(block.arguments, spec.name)
        try:
            arguments = _validate(spec, arguments)
        except ValidationError as exc:
            result = ToolResult(
                content={"error": "argumentos_invalidos", "detail": _readable(exc)}, is_error=True
            )
            invocation = self._invocation(block, spec.kind, result, iteration, started, arguments)
            await self._audit(ctx, invocation)
            return invocation, result

        if spec.kind == "write":
            key = spec.key_for(ctx, arguments)
            previous = seen_keys.get(key)
            if previous is not None:
                # Invariante 5: a mesma escrita, no mesmo turno, devolve o mesmo resultado.
                log.info("tool_escrita_repetida", tool=spec.name)
                invocation = self._invocation(
                    block, spec.kind, previous, iteration, started, arguments, deduplicated=True
                )
                await self._audit(ctx, invocation)
                return invocation, previous

        try:
            result = await spec.handler(ctx, arguments)
        except ToolError as exc:
            result = ToolResult(
                content={"error": "falha_da_ferramenta", "detail": str(exc)}, is_error=True
            )
        except Exception:
            # Detalhe interno nao volta ao modelo — e dele que sai a resposta ao cliente.
            log.exception("tool_excecao_inesperada", tool=spec.name)
            result = ToolResult(content={"error": "falha_interna"}, is_error=True)

        if spec.kind == "write" and not result.is_error:
            seen_keys[spec.key_for(ctx, arguments)] = result

        invocation = self._invocation(block, spec.kind, result, iteration, started, arguments)
        await self._audit(ctx, invocation)
        return invocation, result

    def _invocation(
        self,
        block: ToolUseBlock,
        kind: str,
        result: ToolResult,
        iteration: int,
        started: float,
        arguments: Mapping[str, Any] | None = None,
        *,
        deduplicated: bool = False,
    ) -> ToolInvocation:
        return ToolInvocation(
            name=block.name,
            arguments=dict(arguments if arguments is not None else block.arguments),
            kind=kind,
            ok=not result.is_error,
            iteration=iteration,
            duration_ms=int((time.perf_counter() - started) * 1000),
            deduplicated=deduplicated,
            result=result.content,
        )

    async def _audit(self, ctx: ToolContext, invocation: ToolInvocation) -> None:
        await self.audit.record(
            AuditEntry(
                tenant_id=ctx.tenant_id,
                actor="agent",
                action=f"tool.{invocation.name}",
                entity="conversation",
                entity_id=ctx.conversation_id,
                payload={
                    "arguments": dict(invocation.arguments),
                    "kind": invocation.kind,
                    "ok": invocation.ok,
                    "iteration": invocation.iteration,
                    "duration_ms": invocation.duration_ms,
                    "deduplicated": invocation.deduplicated,
                    "result": dict(invocation.result),
                },
            )
        )

    async def _maybe_summarize(self, request: TurnRequest) -> _SummaryResult | None:
        state = request.state
        if not should_summarize(state.message_count, state.summarized_message_count):
            return None
        text, usage, cost = await summarize(self.provider, state.history, state.summary)
        if not text.strip():
            return None
        return _SummaryResult(text=text, usage=usage, cost=cost)


@dataclass(frozen=True, slots=True)
class _LoopResult:
    reply: str
    usage: Usage
    cost: Decimal
    calls: list[ToolInvocation]
    iterations: int
    model: str
    escalate: bool = False
    escalation_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _SummaryResult:
    text: str
    usage: Usage
    cost: Decimal


# --------------------------------- auxiliares ---------------------------------


def _strip_context_args(arguments: Mapping[str, Any], tool_name: str) -> dict[str, Any]:
    """Remove o que so pode vir do contexto (invariante 3).

    Nao e paranoia teorica: basta o schema de uma tool futura mencionar tenant no texto
    da descricao para o modelo tentar preencher o campo. Aqui ele nunca chega ao handler.
    """
    clean = {k: v for k, v in arguments.items() if k not in _FORBIDDEN_ARGS}
    if len(clean) != len(arguments):
        log.warning("tool_argumento_de_contexto_descartado", tool=tool_name)
    return clean


def _validate(spec: ToolSpec, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Valida com o modelo Pydantic da tool, quando ela declara um."""
    if spec.args_model is None:
        return dict(arguments)
    validated = spec.args_model.model_validate(dict(arguments))
    return validated.model_dump(mode="json", exclude_none=True)


def _readable(exc: ValidationError) -> str:
    """Erro de validacao em uma linha por campo, para o modelo poder se corrigir."""
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
    )


def next_stage(
    current: str,
    *,
    successful_tools: Sequence[str],
    intake_complete: bool,
    escalated: bool,
) -> str:
    """Proximo estagio da conversa (11.3), decidido em codigo.

    `stage` e informativo: orienta o prompt e alimenta o funil, e nao bloqueia nada. Por
    isso a regra pode ser esta escada curta — a primeira condicao verdadeira ganha.
    """
    tools = set(successful_tools)
    if escalated or "escalate_to_human" in tools:
        return "handoff"
    if {"confirm_appointment", "reschedule_appointment"} & tools:
        return "booked"
    if "cancel_appointment" in tools:
        return "answering"
    if "hold_slot" in tools:
        return "confirming"
    if "check_availability" in tools or intake_complete:
        return "offering"
    if current == "greeting":
        return "answering"
    return current
