"""Dubles da suite conversacional: modelo, calendario, canal e handoff.

O que e falso e o que e real
----------------------------
Falso aqui e so a **borda**: o modelo, o calendario, o canal de saida e a tabela de
handoffs. O agente sob teste e o de verdade — `AgentEngine`, `InputGuardrail`,
`OutputGuardrail`, `ToolRegistry` e as tools registradas. Uma suite que trocasse o
agente por uma simulacao mediria a simulacao.

Por que o modelo e roteirizado
------------------------------
A 19.4 manda rodar a suite inteira a cada mudanca de prompt e bloquear o merge na queda
de qualquer cenario. Isso so funciona se a suite for deterministica: um modelo real
responde diferente a cada execucao e transformaria o placar em ruido. O roteiro do YAML
fixa o que o modelo faz; o que a suite mede e o que o **sistema em volta dele** faz com
isso — curto-circuito de gatilho, despacho de tool, conferencia de saida, escalonamento.

Isso tambem permite o teste que a API real nao permite: roteirizar o modelo **errando de
proposito** (citar preco que nao existe, oferecer horario que nenhuma tool devolveu) e
provar que a resposta nao chega ao cliente.

O modo ao vivo (`CONVERSATIONAL_LIVE=1`) troca o roteiro pelo `AnthropicProvider` e serve
ao juiz de tom; ele nao entra no placar, porque nao e deterministico.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.agent.guardrails import InputDecision, InputGuardrail
from app.agent.llm import (
    ExtractionResponse,
    LLMMessage,
    LLMProvider,
    LLMResponse,
    PromptSegment,
    TextBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
)
from app.channels.base import SentMessage
from app.core.time import tz_of

__all__ = [
    "FakeCalendar",
    "FakeChannel",
    "HandoffRecord",
    "InMemoryHandoffs",
    "RecordedPrompt",
    "RecordingInputGuardrail",
    "RecordingProvider",
    "ScriptedModel",
    "ScriptedStep",
]

#: Tokens e custo fixos por chamada. Baixos de proposito: o disjuntor de custo da 11.5
#: nao pode disparar por acidente num cenario que nao e sobre ele.
STEP_USAGE = Usage(input_tokens=800, output_tokens=120)
STEP_COST = Decimal("0.002")


# ---------------------------------- modelo ----------------------------------


@dataclass(frozen=True, slots=True)
class ScriptedStep:
    """Uma resposta do modelo: ou texto, ou uma chamada de tool."""

    say: str | None = None
    tool: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScriptedModel:
    """`LLMProvider` que devolve o roteiro do turno, na ordem.

    Esgotado o roteiro, devolve `fallback` — e nao repete o ultimo passo. Repetir uma
    chamada de tool criaria um laco ate o teto de iteracoes e faria o cenario falhar por
    um motivo que nao e o dele.
    """

    fallback: str
    extraction: Mapping[str, Any] = field(default_factory=dict)
    steps: list[ScriptedStep] = field(default_factory=list)
    exhausted: int = 0
    turn: int = 0
    position: int = 0

    def load(self, steps: Sequence[ScriptedStep]) -> None:
        """Instala o roteiro do proximo turno."""
        self.steps = list(steps)
        self.turn += 1
        self.position = 0

    async def complete(
        self,
        *,
        system: Sequence[PromptSegment],
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSchema] = (),
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        if self.position >= len(self.steps):
            self.exhausted += 1
            return self._response((TextBlock(self.fallback),))

        step = self.steps[self.position]
        self.position += 1
        if step.tool is not None:
            block = ToolUseBlock(
                id=f"tu_{self.turn}_{self.position}",
                name=step.tool,
                arguments=dict(step.arguments),
            )
            return self._response((block,), stop_reason="tool_use")
        return self._response((TextBlock(step.say or ""),))

    async def extract(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        schema: Mapping[str, Any],
        model: str | None = None,
    ) -> ExtractionResponse:
        return ExtractionResponse(
            data=dict(self.extraction),
            model="claude-haiku-4-5",
            usage=Usage(input_tokens=200, output_tokens=30),
            cost_usd=Decimal("0.0002"),
        )

    def _response(
        self, blocks: tuple[TextBlock | ToolUseBlock, ...], *, stop_reason: str = "end_turn"
    ) -> LLMResponse:
        return LLMResponse(
            blocks=blocks,
            stop_reason=stop_reason,
            model="scripted",
            usage=STEP_USAGE,
            cost_usd=STEP_COST,
        )


@dataclass(frozen=True, slots=True)
class RecordedPrompt:
    """O que o modelo viu numa chamada. E aqui que se confere a invariante 7."""

    system: tuple[PromptSegment, ...]
    messages: tuple[LLMMessage, ...]
    tools: tuple[str, ...]

    @property
    def system_text(self) -> str:
        return "\n\n".join(segment.text for segment in self.system)

    @property
    def user_text(self) -> str:
        return "\n".join(message.text for message in self.messages if message.role == "user")


@dataclass(slots=True)
class RecordingProvider:
    """Envelope que grava as chamadas. Serve ao roteiro e ao modelo de verdade."""

    inner: LLMProvider
    prompts: list[RecordedPrompt] = field(default_factory=list)
    extractions: int = 0

    async def complete(
        self,
        *,
        system: Sequence[PromptSegment],
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSchema] = (),
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.prompts.append(
            RecordedPrompt(
                system=tuple(system),
                messages=tuple(messages),
                tools=tuple(tool.name for tool in tools),
            )
        )
        return await self.inner.complete(
            system=system, messages=messages, tools=tools, model=model, max_tokens=max_tokens
        )

    async def extract(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        schema: Mapping[str, Any],
        model: str | None = None,
    ) -> ExtractionResponse:
        self.extractions += 1
        return await self.inner.extract(
            system=system, messages=messages, schema=schema, model=model
        )


# -------------------------------- guardrail --------------------------------


@dataclass(slots=True)
class RecordingInputGuardrail(InputGuardrail):
    """O guardrail de entrada de verdade, guardando o que ele decidiu.

    `InputDecision` morre dentro do motor — `TurnOutcome` nao carrega as marcas de
    injecao. Sem este registro, a assercao `injection_flags` teria de reexecutar
    `detect_injection` por fora, e passaria a testar a si mesma em vez do turno.
    """

    decisions: list[InputDecision] = field(default_factory=list)

    async def check(self, **kwargs: Any) -> InputDecision:
        # `super()` sem argumentos nao funciona aqui: `@dataclass(slots=True)` recria a
        # classe depois de o corpo ser compilado, e a celula de `__class__` que o
        # `super()` implicito usa aponta para a classe antiga.
        decision = await InputGuardrail.check(self, **kwargs)
        self.decisions.append(decision)
        return decision


# -------------------------------- calendario --------------------------------


@dataclass(slots=True)
class FakeCalendar:
    """Agenda em memoria, com instantes ancorados no `now` congelado do cenario.

    Slot escrito no YAML e sempre **relativo** (`+1 14:30`, `hoje 16:00`). Data absoluta
    apodrece: um cenario escrito com `2026-09-09` comeca a testar o passado assim que o
    dia chega, e as regras de data do guardrail de saida mudam de comportamento com ele.
    """

    now: datetime
    timezone: str
    busy: tuple[str, ...] = ()

    def resolve(self, spec: str) -> datetime:
        """`+2 14:30`, `hoje 16:00` ou `amanha 09:00` viram instante local."""
        day_part, _, clock_part = spec.strip().partition(" ")
        if not clock_part:
            raise ValueError(f"slot sem horario: {spec!r}")
        hour, _, minute = clock_part.partition(":")
        local = self.now.astimezone(tz_of(self.timezone))
        return (local + timedelta(days=_day_offset(day_part))).replace(
            hour=int(hour), minute=int(minute or 0), second=0, microsecond=0
        )

    def is_busy(self, spec: str) -> bool:
        return any(self.resolve(spec) == self.resolve(taken) for taken in self.busy)

    def free_slots(self, specs: Sequence[str]) -> list[dict[str, Any]]:
        """Os slots livres, no formato que o guardrail de saida le como evidencia.

        O instante sai em ISO **com offset**: `app.agent.temporal` ignora instante sem
        timezone de proposito, e um slot ignorado viraria horario sem evidencia.
        """
        return [
            {"starts_at": self.resolve(spec).isoformat()}
            for spec in specs
            if not self.is_busy(spec)
        ]


def _day_offset(part: str) -> int:
    if part in ("hoje", "today"):
        return 0
    if part in ("amanha", "tomorrow"):
        return 1
    return int(part)


# ---------------------------------- canal ----------------------------------


@dataclass(slots=True)
class FakeChannel:
    """`OutboundChannel` em memoria. Recebe as respostas e as notificacoes de handoff."""

    sent: list[tuple[str, str]] = field(default_factory=list)
    read_marks: list[str] = field(default_factory=list)

    async def send_text(self, *, to: str, text: str) -> SentMessage:
        self.sent.append((to, text))
        return SentMessage(provider_msg_id=f"fake-{len(self.sent)}")

    async def mark_read(self, *, provider_msg_id: str, typing: bool = False) -> None:
        self.read_marks.append(provider_msg_id)

    async def aclose(self) -> None:
        return None


# --------------------------------- handoff ---------------------------------


@dataclass(frozen=True, slots=True)
class HandoffRecord:
    conversation_id: uuid.UUID
    reason: str
    triggered_by: str
    opened_at: datetime
    silenced_until: datetime


@dataclass(slots=True)
class InMemoryHandoffs:
    """O que `HandoffService` faria, sem Postgres (RF-26..RF-28).

    Guarda as duas regras que mudam o turno seguinte: um handoff aberto por vez e o
    silencio ate `silenced_until`. A persistencia de verdade ja tem teste contra banco
    real (`tests/integration/test_handoff.py`); repeti-la aqui so amarraria a suite
    conversacional ao docker.
    """

    open_records: list[HandoffRecord] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)

    @property
    def current(self) -> HandoffRecord | None:
        return self.open_records[-1] if self.open_records else None

    def silenced(self, now: datetime) -> bool:
        record = self.current
        return record is not None and now < record.silenced_until

    def open(
        self,
        *,
        conversation_id: uuid.UUID,
        reason: str,
        triggered_by: str,
        now: datetime,
        silence_minutes: int,
    ) -> bool:
        """Abre o handoff. Devolve `False` quando ja havia um aberto (idempotencia)."""
        if self.silenced(now):
            return False
        self.open_records.append(
            HandoffRecord(
                conversation_id=conversation_id,
                reason=reason,
                triggered_by=triggered_by,
                opened_at=now,
                silenced_until=now + timedelta(minutes=silence_minutes),
            )
        )
        self.notifications.append(reason)
        return True
