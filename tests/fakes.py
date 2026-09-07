"""Dubles usados pelos testes do motor do agente.

O provider roteirizado existe para uma razao especifica: o loop de tool calling precisa
ser testado com o modelo **insistindo** — repetindo a mesma escrita, pedindo tool que nao
existe, nunca parando. Nada disso e reproduzivel com a API real.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.agent.llm import (
    ExtractionResponse,
    LLMMessage,
    LLMResponse,
    PromptSegment,
    TextBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
)
from app.agent.tools.base import ToolContext, ToolResult, ToolSpec

DEFAULT_USAGE = Usage(input_tokens=1000, output_tokens=200)


def text_response(text: str, *, usage: Usage = DEFAULT_USAGE) -> LLMResponse:
    return LLMResponse(
        blocks=(TextBlock(text),),
        stop_reason="end_turn",
        model="claude-sonnet-5",
        usage=usage,
        cost_usd=Decimal("0.004"),
    )


def tool_response(
    name: str, arguments: Mapping[str, Any], *, call_id: str = "", usage: Usage = DEFAULT_USAGE
) -> LLMResponse:
    return LLMResponse(
        blocks=(
            ToolUseBlock(
                id=call_id or f"tu_{uuid.uuid4().hex[:8]}", name=name, arguments=dict(arguments)
            ),
        ),
        stop_reason="tool_use",
        model="claude-sonnet-5",
        usage=usage,
        cost_usd=Decimal("0.004"),
    )


@dataclass
class RecordedCall:
    system: tuple[PromptSegment, ...]
    messages: tuple[LLMMessage, ...]
    tools: tuple[ToolSchema, ...]


@dataclass
class ScriptedProvider:
    """Devolve respostas na ordem em que foram roteirizadas.

    Esgotado o roteiro, repete a ultima resposta — assim um teste que so quer provar o
    teto de iteracoes escreve uma linha em vez de sete.
    """

    script: list[LLMResponse] = field(default_factory=list)
    extraction: Mapping[str, Any] = field(default_factory=dict)
    calls: list[RecordedCall] = field(default_factory=list)
    extraction_calls: int = 0

    async def complete(
        self,
        *,
        system: Sequence[PromptSegment],
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSchema] = (),
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.calls.append(
            RecordedCall(system=tuple(system), messages=tuple(messages), tools=tuple(tools))
        )
        if not self.script:
            return text_response("(sem roteiro)")
        if len(self.script) == 1:
            return self.script[0]
        return self.script.pop(0)

    async def extract(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        schema: Mapping[str, Any],
        model: str | None = None,
    ) -> ExtractionResponse:
        self.extraction_calls += 1
        return ExtractionResponse(
            data=dict(self.extraction),
            model="claude-haiku-4-5",
            usage=Usage(input_tokens=300, output_tokens=40),
            cost_usd=Decimal("0.0005"),
        )

    @property
    def last_call(self) -> RecordedCall:
        return self.calls[-1]

    @property
    def system_text(self) -> str:
        return "\n\n".join(segment.text for segment in self.last_call.system)


@dataclass
class RecordingTool:
    """Tool falsa que conta execucoes. E o contador que prova a idempotencia."""

    name: str = "fake_tool"
    kind: str = "read"
    result: Mapping[str, Any] = field(default_factory=lambda: {"status": "ok"})
    fail: bool = False
    executions: list[Mapping[str, Any]] = field(default_factory=list)

    async def __call__(self, ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        self.executions.append(dict(arguments))
        if self.fail:
            return ToolResult(content={"error": "boom"}, is_error=True)
        return ToolResult(content=dict(self.result))

    def spec(self, **overrides: Any) -> ToolSpec:
        base: dict[str, Any] = {
            "name": self.name,
            "description": f"tool de teste {self.name}",
            "input_schema": {"type": "object", "properties": {}},
            "handler": self,
            "kind": self.kind,
        }
        base.update(overrides)
        return ToolSpec(**base)
