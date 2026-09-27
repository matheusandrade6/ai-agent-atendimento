"""Loop de tool calling do agente (secao 11.1).

O motor executa os passos 3 a 9 da 11.1. Os passos 1 e 2 sao do worker (S05) e do
carregador de estado (`app.agent.runner`); os guardrails (3 e 8) vivem em
`app.agent.guardrails` e entram por `input_guardrail` / `output_guardrail` — sem eles
instalados o motor roda, mas **sem defesa contra alucinacao de horario**.

Regeneracao e escalonamento (11.5)
----------------------------------
Quando o guardrail de saida descarta a resposta, o motor nao desiste nem envia assim
mesmo: ele devolve ao modelo o que foi apontado e pede a reescrita, com o teto de
iteracoes reduzido. Se a segunda resposta tambem for descartada, o turno escala para
humano. Duas tentativas e o suficiente — um modelo que erra a mesma conferencia duas
vezes nao acerta na terceira, e cada rodada custa dinheiro do teto da conversa.

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
from typing import Any, Final

from pydantic import ValidationError

from app.agent.audit import AuditEntry, AuditSink, NullAuditSink
from app.agent.guardrails import (
    InputGuardrail,
    OutputContext,
    OutputGuardrail,
    business_hour_bounds,
    circuit_breaker,
    prices_from,
    regeneration_hint,
    split_message,
)
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
from app.agent.temporal import gather_evidence
from app.agent.tools.base import ToolContext, ToolError, ToolRegistry, ToolResult, ToolSpec
from app.core.telemetry import get_logger, log_context
from app.domain.tenant_config import TenantConfig

log = get_logger(__name__)

__all__ = [
    "MAX_TOOL_ITERATIONS",
    "REGENERATION_ITERATIONS",
    "AgentEngine",
    "ConversationState",
    "ToolInvocation",
    "TurnOutcome",
    "TurnRequest",
    "last_inbound_text",
    "next_stage",
]

#: Teto de idas e voltas com tools num unico turno (11.1, passo 7).
MAX_TOOL_ITERATIONS: Final[int] = 6

#: Teto da reescrita pedida pelo guardrail de saida. Duas iteracoes: uma para o modelo
#: chamar a tool que faltou, outra para escrever a resposta com o resultado dela.
REGENERATION_ITERATIONS: Final[int] = 2

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
    #: Texto inteiro da resposta. E o que o painel e o log mostram.
    reply: str
    stage: str
    collected: Mapping[str, Any]
    missing: tuple[str, ...]
    usage: Usage
    cost_usd: Decimal
    #: A resposta ja quebrada pelo `max_message_chars` do tenant (11.5). E **esta** a
    #: lista que vai para o canal, em ordem; `reply` e a juncao dela.
    messages: tuple[str, ...] = ()
    tool_calls: tuple[ToolInvocation, ...] = ()
    iterations: int = 0
    summary: str | None = None
    escalate: bool = False
    escalation_reason: str | None = None
    model: str = ""
    #: Tipos de violacao que o guardrail de saida apontou neste turno (para metrica).
    violations: tuple[str, ...] = ()
    #: A resposta precisou ser reescrita depois de ser descartada.
    regenerated: bool = False

    @property
    def used_tools(self) -> tuple[str, ...]:
        return tuple(call.name for call in self.tool_calls)


def last_inbound_text(state: ConversationState) -> str:
    """A rajada que acabou de chegar: as mensagens de entrada apos a ultima resposta.

    E este o texto que o guardrail de entrada examina. Olhar so a ultima mensagem
    perderia o gatilho de emergencia quando a pessoa escreve em tres baloes — que e
    justamente como se escreve quando se esta desesperado.
    """
    parts: list[str] = []
    for message in reversed(tuple(state.history)):
        if message.direction != "inbound":
            break
        parts.append(message.content)
    return "\n".join(reversed(parts))


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
    #: Passo 3 da 11.1. Sem ele, gatilho de escalonamento nao curto-circuita nada.
    input_guardrail: InputGuardrail | None = None
    #: Passo 8 da 11.1. Sem ele, horario e preco saem sem conferencia — e a defesa
    #: contra o risco de maior impacto do projeto (secao 21) fica desligada.
    output_guardrail: OutputGuardrail | None = None

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

        blocked = await self._input_guardrail(request)
        if blocked is not None:
            return blocked

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

        guarded = await self._guard_output(request, loop, system=system, state=state)
        usage += guarded.usage
        cost += guarded.cost
        calls = [*loop.calls, *guarded.calls]

        summary = await self._maybe_summarize(request)
        if summary is not None:
            usage += summary.usage
            cost += summary.cost

        tool_escalation = _tool_escalation_reason(calls)
        escalate = loop.escalate or guarded.escalate or tool_escalation is not None
        stage = next_stage(
            request.state.stage,
            successful_tools=tuple(c.name for c in calls if c.ok),
            intake_complete=state.is_complete,
            escalated=escalate,
        )

        return TurnOutcome(
            reply="\n\n".join(guarded.messages),
            messages=guarded.messages,
            stage=stage,
            collected=state.collected,
            missing=state.missing,
            usage=usage,
            cost_usd=cost,
            tool_calls=tuple(calls),
            iterations=loop.iterations + guarded.iterations,
            summary=summary.text if summary is not None else None,
            escalate=escalate,
            escalation_reason=guarded.reason or loop.escalation_reason or tool_escalation,
            model=guarded.model or loop.model,
            violations=guarded.violations,
            regenerated=guarded.regenerated,
        )

    # ------------------------------ guardrails ------------------------------

    def _circuit_breaker(self, request: TurnRequest) -> TurnOutcome | None:
        """Disjuntor da 11.5: conversa longa ou cara demais vira handoff, sem chamar o LLM."""
        state = request.state
        reason = circuit_breaker(
            request.config.limits,
            message_count=state.message_count,
            cost_usd=state.cost_so_far_usd,
        )
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

    async def _input_guardrail(self, request: TurnRequest) -> TurnOutcome | None:
        """Passo 3 da 11.1. Gatilho que casa nunca chega ao modelo."""
        guard = self.input_guardrail
        if guard is None:
            return None

        text = last_inbound_text(request.state)
        if not text.strip():
            return None

        decision = await guard.check(
            config=request.config,
            text=text,
            tenant_id=request.tenant_id,
            contact_id=request.contact_id,
            signals=_trigger_signals(request),
            placeholders=_placeholders(request),
        )
        if not decision.short_circuits:
            return None

        messages = split_message(decision.reply, request.config.persona.max_message_chars)
        return TurnOutcome(
            reply="\n\n".join(messages),
            messages=messages,
            stage="handoff" if decision.escalates else request.state.stage,
            collected=request.state.collected,
            missing=(),
            usage=Usage(),
            cost_usd=Decimal(0),
            escalate=decision.escalates,
            escalation_reason=decision.reason,
        )

    def _output_context(
        self, request: TurnRequest, calls: Sequence[ToolInvocation]
    ) -> OutputContext:
        """O que o turno provou: resultado de tool, base de conhecimento e catalogo."""
        texts = [
            *(snippet.content for snippet in request.knowledge),
            *(
                f"{service.name} {service.price_line} {service.description}"
                for service in request.services
            ),
        ]
        results = [call.result for call in calls if call.ok]
        timezone = request.config.business_hours.timezone
        today = request.now.date()
        return OutputContext(
            config=request.config,
            today=today,
            evidence=gather_evidence(
                tool_results=results,
                texts=texts,
                business_hours=business_hour_bounds(request.config),
                timezone=timezone,
                today=today,
            ),
            prices=prices_from(tool_results=results, texts=texts),
        )

    async def _guard_output(
        self,
        request: TurnRequest,
        loop: _LoopResult,
        *,
        system: Sequence[PromptSegment],
        state: IntakeState,
    ) -> _GuardedReply:
        """Passo 8 da 11.1, com uma reescrita e escalonamento na segunda falha."""
        guard = self.output_guardrail
        if guard is None:
            return _GuardedReply(messages=(loop.reply.strip(),) if loop.reply.strip() else ())

        decision = guard.check(loop.reply, self._output_context(request, loop.calls))
        if decision.verdict != "regenerate":
            return _GuardedReply(messages=decision.messages, violations=decision.kinds)

        # A instrucao de correcao vai como mensagem de usuario, nao como system prompt:
        # o system prompt e prefixo cacheado (D-21) e mexer nele no meio do turno joga
        # o cache inteiro fora.
        window = loop.window
        if loop.reply.strip():
            window.append(LLMMessage.assistant(loop.reply))
        window.append(LLMMessage.user(regeneration_hint(decision.violations)))

        retry = await self._tool_loop(
            request,
            system=system,
            window=window,
            state=state,
            max_iterations=REGENERATION_ITERATIONS,
        )
        second = guard.check(
            retry.reply, self._output_context(request, [*loop.calls, *retry.calls])
        )
        if second.verdict != "regenerate":
            log.info("guardrail_saida_regenerou", motivos=list(decision.kinds))
            return _GuardedReply(
                messages=second.messages,
                violations=decision.kinds + second.kinds,
                usage=retry.usage,
                cost=retry.cost,
                calls=retry.calls,
                iterations=retry.iterations,
                model=retry.model,
                regenerated=True,
                escalate=retry.escalate,
                reason=retry.escalation_reason,
            )

        # Segunda falha: nao ha terceira tentativa. O que o modelo escreveu nao sai —
        # sai a mensagem de fora de escopo do tenant, e um humano assume (11.5).
        log.warning(
            "guardrail_saida_escalou",
            primeira=list(decision.kinds),
            segunda=list(second.kinds),
        )
        fallback = request.config.messages.out_of_scope.strip()
        return _GuardedReply(
            messages=split_message(fallback, request.config.persona.max_message_chars),
            violations=decision.kinds + second.kinds,
            usage=retry.usage,
            cost=retry.cost,
            calls=retry.calls,
            iterations=retry.iterations,
            model=retry.model,
            regenerated=True,
            escalate=True,
            reason="output_guardrail",
        )

    async def _tool_loop(
        self,
        request: TurnRequest,
        *,
        system: Sequence[PromptSegment],
        window: list[LLMMessage],
        state: IntakeState,
        max_iterations: int | None = None,
    ) -> _LoopResult:
        ceiling = self.max_iterations if max_iterations is None else max_iterations
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
        for iteration in range(1, ceiling + 1):
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
                    window=window,
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
            window=window,
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
    #: A janela como o modelo a viu na ultima chamada. E dela que a regeneracao parte:
    #: reconstruir a janela perderia os `tool_result` que ja foram pagos neste turno.
    window: list[LLMMessage] = field(default_factory=list)
    escalate: bool = False
    escalation_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _GuardedReply:
    """Resultado do passo 8, ja com o custo da reescrita quando ela aconteceu."""

    messages: tuple[str, ...]
    violations: tuple[str, ...] = ()
    usage: Usage = field(default_factory=Usage)
    cost: Decimal = Decimal(0)
    calls: list[ToolInvocation] = field(default_factory=list)
    iterations: int = 0
    model: str = ""
    regenerated: bool = False
    escalate: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _SummaryResult:
    text: str
    usage: Usage
    cost: Decimal


# --------------------------------- auxiliares ---------------------------------


def _trigger_signals(request: TurnRequest) -> dict[str, Any]:
    """Sinais do turno para os gatilhos com `condition` (10.3).

    Sao os numeros que o motor ja tem em maos. `unanswered_questions`, citado no YAML de
    exemplo, depende da contagem de perguntas sem resposta que a S10 introduz junto com
    o handoff; ate la a condicao que o cita simplesmente nao casa — que e a semantica de
    campo ausente de `app.domain.conditions`, e o lado seguro do erro.
    """
    state = request.state
    return {
        "message_count": state.message_count,
        "stage": state.stage,
        "collected_count": len(state.collected),
        "cost_usd": float(state.cost_so_far_usd),
    }


def _placeholders(request: TurnRequest) -> dict[str, str]:
    """Valores para os `{campo}` dos textos configurados do tenant (DEF-01).

    A config entra com os dados da unidade (`{address}`, `{phone}`, `{unit_name}`,
    `{agent_name}`) e o turno acrescenta o que so ele sabe — hoje, o nome do contato.
    Campo vazio fica de fora: placeholder sem valor nao sai literal para o cliente, a rede
    de seguranca de `split_message` remove a frase que o cita.

    A confirmacao de agendamento (S15/S16) usa o mesmo mapa, completado com os campos do
    agendamento (`app.domain.templates.APPOINTMENT_PLACEHOLDERS`).
    """
    values = request.config.template_values()
    contact_name = (request.person.contact_name or "").strip()
    if contact_name:
        values["contact_name"] = contact_name
    return values


def _tool_escalation_reason(calls: Sequence[ToolInvocation]) -> str | None:
    """Motivo da escalada quando o proprio modelo chamou `escalate_to_human` (RF-26).

    Sem isto, o turno so escala por disjuntor, teto de iteracoes ou segunda falha do
    guardrail de saida — nunca porque o modelo decidiu que precisava de um humano. O
    prefixo `tool:` distingue esta origem das demais para `app.agent.handoff` decidir
    `triggered_by` sem precisar reabrir o resultado da tool.
    """
    for call in calls:
        if call.ok and call.name == "escalate_to_human":
            reason = call.result.get("reason") if isinstance(call.result, Mapping) else None
            return f"tool:{reason}" if reason else "tool:escalate_to_human"
    return None


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
